#!/usr/bin/env python3
import inspect
import os
import subprocess
import time
from datetime import datetime
from datetime import timezone
from multiprocessing import Process
from subprocess import PIPE

env = dict(os.environ)
env['PATH'] = f'{env["PATH"]}:/usr/local/bin'
env['BRANCH'] = 'master' if not env.get('BRANCH') else env['BRANCH']


def _print_line_number(number_of_outer_frame=1):
    cf = inspect.currentframe()
    frame = cf
    for ii in range(number_of_outer_frame):
        frame = frame.f_back

    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    print('\n'.join(['#' * 40, f'[{timestamp}] LINE NUMBER: {frame.f_lineno}', '#' * 40]))


def _run(cmd, file_path_name=None, cwd=None, file_mode='a'):
    def _f():
        if not file_path_name:
            _p = subprocess.Popen(cmd, cwd=cwd, env=env)
            _p.communicate()
            if _p.returncode != 0:
                raise Exception()
        else:
            with open(file_path_name, file_mode) as ff:
                _p = subprocess.Popen(cmd, stdout=ff, cwd=cwd, env=env)
                _p.communicate()
                if _p.returncode != 0:
                    raise Exception()

    _print_line_number(2)
    cmd_string = ' '.join(cmd)
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    print('\n'.join(['#' * 40, f'[{timestamp}] COMMAND: {cmd_string}', '#' * 40]))

    pp = Process(target=_f)
    pp.start()
    pp.join()
    if pp.exitcode != 0:
        raise Exception()


def _preprocess_vagrant():
    _print_line_number()

    _run(['cp', '--backup', '/vagrant/configuration/root/.bashrc', '/root/.bashrc'])

    hostname = 'tabris-my-local-1a-012345.localdomain'
    _run(['sudo', 'hostnamectl', 'set-hostname', hostname])

    _print_line_number()

    with open('/etc/server_info', 'w') as ff:
        ff.write('AWS_EC2_INSTANCE_ID=i-01234567\n')
        ff.write('AWS_EC2_AVAILABILITY_ZONE=my-local-1a\n')

    _print_line_number()

    _run(['fallocate', '-l', '2G', '/swapfile'])
    _run(['chmod', '600', '/swapfile'])
    _run(['mkswap', '/swapfile'])
    _run(['swapon', '/swapfile'])
    with open('/etc/fstab', 'a') as ff:
        ff.write('/swapfile\tswap\tswap\tsw\t0\t0\n')

    _print_line_number()

    subprocess.Popen(['chpasswd'], stdin=PIPE).communicate(b'root:1234qwer')

    _print_line_number()

    pp = 'etc/systemd/network/20-vagrant-enp0s6.network'
    _run(['cp', '--backup', f'/vagrant/configuration/{pp}', f'/{pp}'])
    _run(['chmod', '644', f'/{pp}'])
    _run(['chown', 'systemd-network:systemd-network', f'/{pp}'])


def _setup_venv():
    _print_line_number()

    venv_path = '/opt/tabris/venv'
    python3_12 = '/usr/bin/python3.12'

    _run([python3_12, '-m', 'venv', venv_path])

    venv_pip = f'{venv_path}/bin/pip'

    _run([venv_pip, 'install', '--upgrade', 'pip'])

    requirements_paths = ['/opt/tabris/requirements.txt', '/vagrant/requirements.txt']
    requirements_file = None
    for req_path in requirements_paths:
        if os.path.exists(req_path):
            requirements_file = req_path
            break

    if requirements_file:
        with open(requirements_file) as ff:
            for ll in ff.readlines():
                ll_stripped = ll.strip()
                if ll_stripped and not ll_stripped.startswith('#'):
                    _run([venv_pip, 'install', ll_stripped])


def _setup_docker():
    _print_line_number()

    _run(['dnf', '-y', 'install', 'docker'])
    _run(['systemctl', 'enable', 'docker'])
    _run(['systemctl', 'start', 'docker'])

    _run(['docker', 'build', '-t', 'hbsmith-claude-sandbox', '/opt/tabris'])


def main():
    _print_line_number()

    _preprocess_vagrant()
    _print_line_number()

    _run(['mkdir', '-p', '/root/.ssh'])
    _run(['mkdir', '-p', '/etc/tabris'])
    _run(['mkdir', '-p', '/var/log/tabris'])

    _print_line_number()

    cmd_common = ['cp', '--backup']
    file_list = list()
    file_list.append('/root/.ssh/id_ed25519')
    file_list.append('/etc/systemd/system/tabris.service')

    for ff in file_list:
        dd = '/vagrant/configuration'
        cmd = cmd_common + [dd + ff, ff]
        _run(cmd)

    gitconfig_real = '/vagrant/configuration/root/.gitconfig'
    gitconfig_sample = '/vagrant/configuration/root/.gitconfig_sample'
    if os.path.exists(gitconfig_real):
        _run(['cp', '--backup', gitconfig_real, '/root/.gitconfig'])
    elif os.path.exists(gitconfig_sample):
        _run(['cp', '--backup', gitconfig_sample, '/root/.gitconfig'])

    _print_line_number()

    settings_src_real = '/vagrant/configuration/etc/tabris/settings_local.py'
    settings_src_example = '/vagrant/configuration/etc/tabris/settings_local.py.example'
    settings_dst = '/etc/tabris/settings_local.py'
    if os.path.exists(settings_src_real):
        _run(['cp', '--backup', settings_src_real, settings_dst])
    else:
        print('WARNING: settings_local.py not found in configuration; copying .example as placeholder.')
        print('         Fill in /etc/tabris/settings_local.py on the VM before starting tabris.service.')
        _run(['cp', '--backup', settings_src_example, settings_dst])
    _run(['chmod', '600', settings_dst])
    _run(['chown', 'root:root', settings_dst])

    _print_line_number()

    _run(['chmod', '600', '/root/.ssh/id_ed25519'])
    is_success = False
    for ii in range(10):
        print(f'Git clone try count: {ii + 1}')
        # noinspection PyBroadException
        try:
            _run(['ssh-keyscan', 'github.com'], '/root/.ssh/known_hosts')
            print(f'branch: {env["BRANCH"]}')
            _run(
                ['git', 'clone', '--depth=1', '-b', env['BRANCH'], 'git@github.com:HardBoiledSmith/tabris.git'],
                cwd='/opt',
            )
            if os.path.exists('/opt/tabris'):
                is_success = True
                break
        except Exception:
            time.sleep(3)

    if not is_success:
        raise Exception()

    _print_line_number()

    _setup_venv()

    _print_line_number()

    _setup_docker()

    _print_line_number()

    with open('/etc/logrotate.d/tabris', 'w') as f:
        f.write('/var/log/tabris/*.log {\n')
        f.write('    rotate 3\n')
        f.write('    size 5M\n')
        f.write('    missingok\n')
        f.write('    notifempty\n')
        f.write('    compress\n')
        f.write('    delaycompress\n')
        f.write('    copytruncate\n')
        f.write('}\n')

    _run(['logrotate', '-f', '/etc/logrotate.d/tabris'])

    _print_line_number()

    _run(['systemctl', 'daemon-reload'])
    _run(['systemctl', 'enable', 'tabris.service'])
    _run(['systemctl', 'start', 'tabris.service'])


if __name__ == '__main__':
    main()
