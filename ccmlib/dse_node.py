# ccm node
from __future__ import with_statement

from six import print_

import os
import shutil
import stat
import subprocess
import time
import yaml
import signal

from ccmlib.node import Node
from ccmlib.node import NodeError
from ccmlib import common

class DseNode(Node):
    """
    Provides interactions to a DSE node.
    """

    def __init__(self, name, cluster, auto_bootstrap, thrift_interface, storage_interface, jmx_port, remote_debug_port, initial_token, save=True, binary_interface=None):
        super(DseNode, self).__init__(name, cluster, auto_bootstrap, thrift_interface, storage_interface, jmx_port, remote_debug_port, initial_token, save, binary_interface)
        self.get_cassandra_version()
        if self.cluster.hasOpscenter():
            self._copy_agent()

    def get_install_cassandra_root(self):
        return os.path.join(self.get_install_dir(), 'resources', 'cassandra')

    def get_node_cassandra_root(self):
        return os.path.join(self.get_path(), 'resources', 'cassandra')

    def get_conf_dir(self):
        """
        Returns the path to the directory where Cassandra config are located
        """
        return os.path.join(self.get_path(), 'resources', 'cassandra', 'conf')

    def get_tool(self, toolname):
        return common.join_bin(os.path.join(self.get_install_dir(), 'resources', 'cassandra'), 'bin', toolname)

    def get_tool_args(self, toolname):
        return [common.join_bin(os.path.join(self.get_install_dir(), 'resources', 'cassandra'), 'bin', 'dse'), toolname]

    def get_env(self):
        return common.make_dse_env(self.get_install_dir(), self.get_path())

    def get_cassandra_version(self):
        return common.get_dse_cassandra_version(self.get_install_dir())

    def set_workload(self, workload):
        self.workload = workload
        self._update_config()

    def start(self,
              join_ring=True,
              no_wait=False,
              verbose=False,
              update_pid=True,
              wait_other_notice=False,
              replace_token=None,
              replace_address=None,
              jvm_args=[],
              wait_for_binary_proto=False,
              profile_options=None,
              use_jna=False):
        """
        Start the node. Options includes:
          - join_ring: if false, start the node with -Dcassandra.join_ring=False
          - no_wait: by default, this method returns when the node is started and listening to clients.
            If no_wait=True, the method returns sooner.
          - wait_other_notice: if True, this method returns only when all other live node of the cluster
            have marked this node UP.
          - replace_token: start the node with the -Dcassandra.replace_token option.
          - replace_address: start the node with the -Dcassandra.replace_address option.
        """

        if self.is_running():
            raise NodeError("%s is already running" % self.name)

        for itf in list(self.network_interfaces.values()):
            if itf is not None and replace_address is None:
                common.check_socket_available(itf)

        if wait_other_notice:
            marks = [ (node, node.mark_log()) for node in list(self.cluster.nodes.values()) if node.is_running() ]


        cdir = self.get_install_dir()
        launch_bin = common.join_bin(cdir, 'bin', 'dse')
        # Copy back the dse scripts since profiling may have modified it the previous time
        shutil.copy(launch_bin, self.get_bin_dir())
        launch_bin = common.join_bin(self.get_path(), 'bin', 'dse')

        # If Windows, change entries in .bat file to split conf from binaries
        if common.is_win():
            self.__clean_bat()

        if profile_options is not None:
            config = common.get_config()
            if not 'yourkit_agent' in config:
                raise NodeError("Cannot enable profile. You need to set 'yourkit_agent' to the path of your agent in a ~/.ccm/config")
            cmd = '-agentpath:%s' % config['yourkit_agent']
            if 'options' in profile_options:
                cmd = cmd + '=' + profile_options['options']
            print_(cmd)
            # Yes, it's fragile as shit
            pattern=r'cassandra_parms="-Dlog4j.configuration=log4j-server.properties -Dlog4j.defaultInitOverride=true'
            common.replace_in_file(launch_bin, pattern, '    ' + pattern + ' ' + cmd + '"')

        os.chmod(launch_bin, os.stat(launch_bin).st_mode | stat.S_IEXEC)

        env = common.make_dse_env(self.get_install_dir(), self.get_path())

        if common.is_win():
            self._clean_win_jmx();

        pidfile = os.path.join(self.get_path(), 'cassandra.pid')
        args = [launch_bin, 'cassandra']

        if self.workload is not None:
            if 'hadoop' in self.workload:
                args.append('-t')
            if 'solr' in self.workload:
                args.append('-s')
            if 'spark' in self.workload:
                args.append('-k')
            if 'cfs' in self.workload:
                args.append('-c')
        args += [ '-p', pidfile, '-Dcassandra.join_ring=%s' % str(join_ring) ]
        if replace_token is not None:
            args.append('-Dcassandra.replace_token=%s' % str(replace_token))
        if replace_address is not None:
            args.append('-Dcassandra.replace_address=%s' % str(replace_address))
        if use_jna is False:
            args.append('-Dcassandra.boot_without_jna=true')
        args = args + jvm_args

        process = None
        if common.is_win():
            # clean up any old dirty_pid files from prior runs
            if (os.path.isfile(self.get_path() + "/dirty_pid.tmp")):
                os.remove(self.get_path() + "/dirty_pid.tmp")
            process = subprocess.Popen(args, cwd=self.get_bin_dir(), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        else:
            process = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Our modified batch file writes a dirty output with more than just the pid - clean it to get in parity
        # with *nix operation here.
        if common.is_win():
            self.__clean_win_pid()
            self._update_pid(process)
        elif update_pid:
            if no_wait:
                time.sleep(2) # waiting 2 seconds nevertheless to check for early errors and for the pid to be set
            else:
                for line in process.stdout:
                    if verbose:
                        print_(line.rstrip('\n'))

            self._update_pid(process)

            if not self.is_running():
                raise NodeError("Error starting node %s" % self.name, process)

        if wait_other_notice:
            for node, mark in marks:
                node.watch_log_for_alive(self, from_mark=mark)

        if wait_for_binary_proto:
            self.watch_log_for("Starting listening for CQL clients")
            # we're probably fine at that point but just wait some tiny bit more because
            # the msg is logged just before starting the binary protocol server
            time.sleep(0.2)

        if self.cluster.hasOpscenter():
            self._start_agent()

        return process

    def stop(self, wait=True, wait_other_notice=False, gently=True):
        stopped = super(DseNode, self).stop(wait, wait_other_notice, gently)
        if self.cluster.hasOpscenter():
            self._stop_agent()
        return stopped

    def dsetool(self, cmd):
        env = common.make_dse_env(self.get_install_dir(), self.get_path())
        host = self.address()
        dsetool = common.join_bin(self.get_install_dir(), 'bin', 'dsetool')
        args = [dsetool, '-h', host, '-j', str(self.jmx_port)]
        args += cmd.split()
        p = subprocess.Popen(args, env=env)
        p.wait()

    def hadoop(self, hadoop_options=[]):
        env = common.make_dse_env(self.get_install_dir(), self.get_path())
        dse = common.join_bin(self.get_install_dir(), 'bin', 'dse')
        args = [dse, 'hadoop']
        args += hadoop_options
        p = subprocess.Popen(args, env=env)
        p.wait()

    def hive(self, hive_options=[]):
        env = common.make_dse_env(self.get_install_dir(), self.get_path())
        dse = common.join_bin(self.get_install_dir(), 'bin', 'dse')
        args = [dse, 'hive']
        args += hive_options
        p = subprocess.Popen(args, env=env)
        p.wait()

    def pig(self, pig_options=[]):
        env = common.make_dse_env(self.get_install_dir(), self.get_path())
        dse = common.join_bin(self.get_install_dir(), 'bin', 'dse')
        args = [dse, 'pig']
        args += pig_options
        p = subprocess.Popen(args, env=env)
        p.wait()

    def sqoop(self, sqoop_options=[]):
        env = common.make_dse_env(self.get_install_dir(), self.get_path())
        dse = common.join_bin(self.get_install_dir(), 'bin', 'dse')
        args = [dse, 'sqoop']
        args += sqoop_options
        p = subprocess.Popen(args, env=env)
        p.wait()

    def import_dse_config_files(self):
        self._update_config()
        if not os.path.isdir(os.path.join(self.get_path(), 'resources', 'dse', 'conf')):
            os.makedirs(os.path.join(self.get_path(), 'resources', 'dse', 'conf'))
        common.copy_directory(os.path.join(self.get_install_dir(), 'resources', 'dse', 'conf'), os.path.join(self.get_path(), 'resources', 'dse', 'conf'))
        self.__update_yaml()

    def copy_config_files(self):
        for product in ['dse', 'cassandra', 'hadoop', 'sqoop', 'hive', 'tomcat', 'spark', 'shark', 'mahout', 'pig']:
            if not os.path.isdir(os.path.join(self.get_path(), 'resources', product, 'conf')):
                os.makedirs(os.path.join(self.get_path(), 'resources', product, 'conf'))
            common.copy_directory(os.path.join(self.get_install_dir(), 'resources', product, 'conf'), os.path.join(self.get_path(), 'resources', product, 'conf'))
            if product == 'cassandra':
                os.mkdir(os.path.join(self.get_path(), 'resources', product, 'conf', 'triggers'))

    def import_bin_files(self):
        os.makedirs(os.path.join(self.get_path(), 'resources', 'cassandra', 'bin'))
        common.copy_directory(os.path.join(self.get_install_dir(), 'bin'), self.get_bin_dir())
        common.copy_directory(os.path.join(self.get_install_dir(), 'resources', 'cassandra', 'bin'), os.path.join(self.get_path(), 'resources', 'cassandra', 'bin'))

    def __update_yaml(self):
        conf_file = os.path.join(self.get_path(), 'resources', 'dse', 'conf', 'dse.yaml')
        with open(conf_file, 'r') as f:
            data = yaml.load(f)

        data['system_key_directory'] = os.path.join(self.get_path(), 'keys')

        full_options = dict(list(self.cluster._dse_config_options.items()))
        for name in full_options:
            if not name is 'dse_yaml_file':
                value = full_options[name]
                if value is None:
                    try:
                        del data[name]
                    except KeyError:
                        # it is fine to remove a key not there:w
                        pass
                else:
                    data[name] = full_options[name]

        if 'dse_yaml_file' in full_options:
            with open(full_options['dse_yaml_file'], 'r') as f:
                user_yaml = yaml.load(f)
                data = common.yaml_merge(data, user_yaml)

        with open(conf_file, 'w') as f:
            yaml.safe_dump(data, f, default_flow_style=False)

    def _get_directories(self):
        dirs = {}
        for i in ['data', 'commitlogs', 'saved_caches', 'logs', 'bin', 'keys', 'resources']:
            dirs[i] = os.path.join(self.get_path(), i)
        return dirs

    def _copy_agent(self):
        agent_source = os.path.join(self.get_install_dir(), 'datastax-agent')
        agent_target = os.path.join(self.get_path(), 'datastax-agent')
        if os.path.exists(agent_source) and not os.path.exists(agent_target):
            shutil.copytree(agent_source, agent_target)

    def _start_agent(self):
        agent_dir = os.path.join(self.get_path(), 'datastax-agent')
        if os.path.exists(agent_dir):
            self._write_agent_address_yaml(agent_dir)
            self._write_agent_log4j_properties(agent_dir)
            args = [os.path.join(agent_dir, 'bin', common.platform_binary('datastax-agent'))]
            subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _stop_agent(self):
        agent_dir = os.path.join(self.get_path(), 'datastax-agent')
        if os.path.exists(agent_dir):
            pidfile = os.path.join(agent_dir, 'datastax-agent.pid')
        if os.path.exists(pidfile):
            with open(pidfile, 'r') as f:
                pid = int(f.readline().strip())
                f.close()
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            os.remove(pidfile)

    def _write_agent_address_yaml(self, agent_dir):
        address_yaml = os.path.join(agent_dir, 'conf', 'address.yaml')
        if not os.path.exists(address_yaml):
            with open(address_yaml, 'w+') as f:
                (ip, port) = self.network_interfaces['thrift']
                jmx = self.jmx_port
                f.write('stomp_interface: 127.0.0.1\n')
                f.write('local_interface: %s\n' % ip)
                f.write('agent_rpc_interface: %s\n' % ip)
                f.write('agent_rpc_broadcast_address: %s\n' % ip)
                f.write('cassandra_conf: %s\n' % os.path.join(self.get_path(), 'resources', 'cassandra', 'conf', 'cassandra.yaml'))
                f.write('cassandra_install: %s\n' % self.get_path())
                f.write('cassandra_logs: %s\n' % os.path.join(self.get_path(), 'logs'))
                f.write('thrift_port: %s\n' % port)
                f.write('jmx_port: %s\n' % jmx)
                f.close()

    def _write_agent_log4j_properties(self, agent_dir):
        log4j_properties = os.path.join(agent_dir, 'conf', 'log4j.properties')
        with open(log4j_properties, 'w+') as f:
            f.write('log4j.rootLogger=INFO,R\n')
            f.write('log4j.logger.org.apache.http=OFF\n')
            f.write('log4j.logger.org.eclipse.jetty.util.log=WARN,R\n')
            f.write('log4j.appender.R=org.apache.log4j.RollingFileAppender\n')
            f.write('log4j.appender.R.maxFileSize=20MB\n')
            f.write('log4j.appender.R.maxBackupIndex=5\n')
            f.write('log4j.appender.R.layout=org.apache.log4j.PatternLayout\n')
            f.write('log4j.appender.R.layout.ConversionPattern=%5p [%t] %d{ISO8601} %m%n\n')
            f.write('log4j.appender.R.File=./log/agent.log\n')
            f.close()
