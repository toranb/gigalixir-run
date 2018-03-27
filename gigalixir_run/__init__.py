import sys
import urlparse
import glob
from functools import wraps
import contextlib
import tarfile
import requests
import rollbar
import logging
import subprocess
import os
import json
import click
import urllib3.contrib.pyopenssl
import signal
import pystache
urllib3.contrib.pyopenssl.inject_into_urllib3()

@click.group()
@click.option('--env', envvar='GIGALIXIR_ENV', default='prod', help="GIGALIXIR environment [prod, dev].")
@click.pass_context
def cli(ctx, env):
    ctx.obj = {}
    ROLLBAR_POST_CLIENT_ITEM = "2413770c24624d498a9baa91d15f7ece"
    if env == "prod":
        rollbar.init(ROLLBAR_POST_CLIENT_ITEM, 'production', enabled=True)
        host = "https://api.gigalixir.com"
    elif env == "dev":
        rollbar.init(ROLLBAR_POST_CLIENT_ITEM, 'development', enabled=False)
        host = "http://localhost:4000"
    else:
        raise Exception("Invalid GIGALIXIR_ENV")

    ctx.obj['host'] = host

def report_errors(f):
    @wraps(f)
    def wrapper(*args, **kwds):
        try:
            f(*args, **kwds)
        except SystemExit:
            raise
        except:
            rollbar.report_exc_info()
            raise
    return wrapper

@cli.command()
@click.argument('repo', nargs=1)
@click.argument('cmd', nargs=-1)
@click.option('--app_key', envvar='APP_KEY', default=None)
@click.pass_context
@report_errors
def init(ctx, repo, cmd, app_key):
    if app_key == None:
        raise Exception("APP_KEY not found.")

    # Upstart, systemd, etc do not run in docker containers, nor do I want them to. 
    # We start the ssh server manually on init. This is not an ideal solution, but
    # is a fine place to start. If the SSH server dies it won't respawn, but I think
    # that is okay for now.
    # SSH is needed for observer and remote_console.
    # Cron is needed to update ssh keys>
    subprocess.check_call(['service', 'ssh', 'start'])

    ssh_config = "/root/.ssh"
    if not os.path.exists(ssh_config):
        os.makedirs(ssh_config)

    # I can't bake this into the image because the env vars are not available at build time. We set it up here
    # at container startup.
    update_authorized_keys_cmd = "curl https://api.gigalixir.com/api/apps/%s/ssh_keys -u %s:%s | jq -r '.data | .[]' > /root/.ssh/authorized_keys" % (repo, repo, app_key)

    subprocess.check_call(['/bin/bash', '-c', update_authorized_keys_cmd])

    p = subprocess.Popen(['crontab'], stdin=subprocess.PIPE)
    p.communicate("* * * * * %s && echo $(date) >> /var/log/cron.log\n" % update_authorized_keys_cmd)
    p.stdin.close()

    subprocess.check_call(['cron'])

    r = requests.get("%s/api/apps/%s/releases/current" % (ctx.obj['host'], repo), auth = (repo, app_key)) 
    if r.status_code != 200:
        raise Exception(r)
    release = r.json()["data"]
    slug_url = release["slug_url"]
    config = release["config"]
    customer_app_name = release["customer_app_name"]

    for key, value in config.iteritems():
        os.environ[key] = value

    # HACK ALERT 
    # Save important env variables for observer and run-cmd when it SSHes in.
    # Normally these variables are set by the pod spec and injected by kubernetes, but 
    # if you SSH into the container manually, they are not set.
    # On container startup, we save these variables to the filesystem for use later when
    # you SSH in.
    kube_var_path = "/kube-env-vars"
    if not os.path.exists(kube_var_path):
        os.makedirs(kube_var_path)
    with open('%s/MY_POD_IP' % kube_var_path, 'w') as f:
        f.write(os.environ['MY_POD_IP'])
    with open('%s/ERLANG_COOKIE' % kube_var_path, 'w') as f:
        f.write(os.environ['ERLANG_COOKIE'])
    with open('%s/REPO' % kube_var_path, 'w') as f:
        f.write(repo)
    with open('%s/APP' % kube_var_path, 'w') as f:
        f.write(customer_app_name)
    with open('%s/APP_KEY' % kube_var_path, 'w') as f:
        f.write(app_key)
    with open('%s/LOGPLEX_TOKEN' % kube_var_path, 'w') as f:
        f.write(os.environ['LOGPLEX_TOKEN'])

    download_file(slug_url, "/app/%s.tar.gz" % customer_app_name)

    click.echo("extracting")
    with cd("/app"):
        tar = tarfile.open("%s.tar.gz" % customer_app_name, "r:gz")
        tar.extractall()
        tar.close()

    epmd_path = find('epmd', '/app')
    os.symlink(epmd_path, '/usr/local/bin/epmd')
    launch(ctx, cmd, use_procfile=True)

@cli.command()
@click.argument('cmd', nargs=-1)
@click.pass_context
@report_errors
def run(ctx, cmd):
    launch(ctx, cmd, log_shuttle=False)


def generate_vmargs(node_name, cookie):
    script_dir = os.path.dirname(__file__) #<-- absolute dir the script is in
    rel_path = "templates/vm.args.mustache"
    template_path = os.path.join(script_dir, rel_path)
    vmargs_path = "/release-config/vm.args"

    with open(template_path, "r") as f:
        template = f.read()
        vmargs = pystache.render(template, {"MY_NODE_NAME": node_name, "MY_COOKIE": cookie})
        with open(vmargs_path, "w") as g:
            g.write(vmargs)

@cli.command()
@click.argument('customer_app_name', nargs=1)
@click.argument('slug_url', nargs=1)
@click.argument('cmd', nargs=-1)
@click.pass_context
@report_errors
def bootstrap(ctx, customer_app_name, slug_url, cmd):
    # Similar to init except does not ask api.gigalixir.com for the current slug url or configs.
    # This also does not support SSH, observer, etc.
    download_file(slug_url, "/app/%s.tar.gz" % customer_app_name)
    with cd("/app"):
        tar = tarfile.open("%s.tar.gz" % customer_app_name, "r:gz")
        tar.extractall()
        tar.close()
    with cd('/app'):
        os.execv('/app/bin/%s' % customer_app_name, ['/app/bin/%s' % customer_app_name] + list(cmd))

@cli.command()
@click.argument('version')
@click.pass_context
@report_errors
def upgrade(ctx, version):
    kube_var_path = "/kube-env-vars"
    with open('%s/APP' % kube_var_path, 'r') as f:
        app = f.read()
    with open('%s/REPO' % kube_var_path, 'r') as f:
        repo = f.read()
    with open('%s/APP_KEY' % kube_var_path, 'r') as f:
        app_key = f.read()

    r = requests.get("%s/api/apps/%s/releases/current" % (ctx.obj['host'], repo), auth = (repo, app_key)) 
    if r.status_code != 200:
        raise Exception(r)
    release = r.json()["data"]
    slug_url = release["slug_url"]

    # get mix version from slug url. 
    # TODO: make this explicit in the database.
    # https://storage.googleapis.com/slug-bucket/production/sunny-wellgroomed-africanpiedkingfisher/releases/0.0.2/SHA/gigalixir_getting_started.tar.gz
    mix_version = urlparse.urlparse(slug_url).path.split('/')[5]
    config = release["config"]

    release_dir = "/app/releases/%s" % mix_version
    if not os.path.exists(release_dir):
        os.makedirs(release_dir)

    for key, value in config.iteritems():
        os.environ[key] = value

    download_file(slug_url, "/app/releases/%s/%s.tar.gz" % (mix_version, app))
    with cd("/app/releases/%s" % mix_version):
        tar = tarfile.open("%s.tar.gz" % app, "r:gz")
        tar.extractall()
        tar.close()

    launch(ctx, ('upgrade', mix_version))

def launch(ctx, cmd, log_shuttle=True, use_procfile=False):
    click.echo("launching")
    # These vars are set by the pod spec and are present EXCEPT when you ssh in manually
    # as is the case when you run remote observer or want a remote_console. In those cases
    # we pull them from the file system instead. It's a bit of a hack. The init script
    # creates those files.
    kube_var_path = "/kube-env-vars"
    with open('%s/MY_POD_IP' % kube_var_path, 'r') as f:
        ip = f.read()
    with open('%s/ERLANG_COOKIE' % kube_var_path, 'r') as f:
        erlang_cookie = f.read()
    with open('%s/REPO' % kube_var_path, 'r') as f:
        repo = f.read()
    with open('%s/APP_KEY' % kube_var_path, 'r') as f:
        app_key = f.read()
    with open('%s/APP' % kube_var_path, 'r') as f:
        app = f.read()
    with open('%s/LOGPLEX_TOKEN' % kube_var_path, 'r') as f:
        logplex_token = f.read()
    os.environ['GIGALIXIR_DEFAULT_VMARGS'] = "true"
    os.environ['REPLACE_OS_VARS'] = "true"
    os.environ['RELX_REPLACE_OS_VARS'] = "true"
    os.environ['MY_NODE_NAME'] = "%s@%s" % (repo, ip)
    os.environ['MY_COOKIE'] = erlang_cookie
    os.environ['LC_ALL'] = "en_US.UTF-8"
    os.environ['LIBCLUSTER_KUBERNETES_SELECTOR'] = "repo=%s" % repo
    os.environ['LIBCLUSTER_KUBERNETES_NODE_BASENAME'] = repo
    os.environ['LOGPLEX_TOKEN'] = logplex_token

    # this is sort of dangerous. the current release
    # might have changed between here and when init
    # was called. that could cause some confusion..
    # TODO: fetch the right release version from disk.
    # TODO: upgrade should update the release version.
    r = requests.get("%s/api/apps/%s/releases/current" % (ctx.obj['host'], repo), auth = (repo, app_key)) 
    if r.status_code != 200:
        raise Exception(r)
    release = r.json()["data"]
    config = release["config"]

    for key, value in config.iteritems():
        os.environ[key] = value
    port = os.environ.get('PORT')

    if os.environ['GIGALIXIR_DEFAULT_VMARGS'].lower() == "true":
        # bypass all the distillery vm.args stuff and use our own
        # we manually set VMARGS_PATH to say to distillery, use this one
        # not any of the million other possible vm.args
        # this means we have to do variable substitution ourselves though =(
        generate_vmargs(os.environ['MY_NODE_NAME'], os.environ['MY_COOKIE'])

        # this needs to be here instead of in the kubernetes spec because
        # we need it for all commands e.g. remote_console, not just init
        # os.environ['RELEASE_CONFIG_DIR'] = "/release-config"
        os.environ['VMARGS_PATH'] = "/release-config/vm.args"

    with cd('/app'):
        os.environ['GIGALIXIR_APP_NAME'] = app
        os.environ['GIGALIXIR_COMMAND'] = ' '.join(cmd)
        os.environ['PYTHONIOENCODING'] = 'utf-8'

        load_profile(os.getcwd())

        if log_shuttle == True:
            appname = repo
            hostname = subprocess.check_output(["hostname"]).strip()

            # send some info through the log shuttle really quick to inform the customer
            # that their app is attempting to start.
            # port is None when running remote_console or something like that.
            if port != None:
                log(logplex_token, appname, hostname, "Attempting to start '%s' on host '%s'\nAttempting health checks on port %s\n" % (appname, hostname, port))

                # log when shutting down.
                def handle_sigterm(signum, frame):
                    log(logplex_token, appname, hostname, "Shutting down '%s' on host '%s'\n" % (appname, hostname))
                    sys.exit(0)
                signal.signal(signal.SIGTERM, handle_sigterm)

            procid = ' '.join(cmd)
            log_shuttle_cmd = "/opt/gigalixir/bin/log-shuttle -logs-url=http://token:%s@post.logs.gigalixir.com/logs -appname %s -hostname %s -procid %s -num-outlets 1 -batch-size=5 -back-buff=5000" % (logplex_token, appname, hostname, hostname)
            if use_procfile:
                # when you use -f, foreman changes the current working dir
                # to the folder the Procfile is in. We set `-d .` to keep
                # it the current dir.
                ps = subprocess.Popen(['foreman', 'start', '-d', '.', '--color', '--no-timestamp', '-f', procfile_path(os.getcwd())], stdout=subprocess.PIPE)
            else:
                ps = subprocess.Popen(['/app/bin/%s' % app] + list(cmd), stdout=subprocess.PIPE)
            subprocess.check_call(log_shuttle_cmd.split(), stdin=ps.stdout)
            ps.wait()
        else:
            if use_procfile:
                # is this ever used?
                os.execv('/app/bin/%s' % app, ['foreman', 'start', '-d', '.', '--color', '--no-timestamp', '-f', procfile_path(os.getcwd())])
            else:
                os.execv('/app/bin/%s' % app, ['/app/bin/%s' % app] + list(cmd))

def procfile_path(cwd):
    if not os.path.exists("%s/Procfile" % cwd):
        return '/opt/gigalixir/Procfile'
    else:
        return 'Procfile'

def load_profile(cwd):
    click.echo("loading profile")
    for f in glob.glob("%s/.profile.d/*.sh" % cwd):
        source(f)

# from http://pythonwise.blogspot.com/2010/04/sourcing-shell-script.html
def source(script, update=1):
    click.echo("sourcing %s" % script)
    pipe = subprocess.Popen(". %s; env" % script, stdout=subprocess.PIPE, shell=True)
    data = pipe.communicate()[0]

    env = dict((line.split("=", 1) for line in data.splitlines()))
    if update:
        os.environ.update(env)

    return env

def log(logplex_token, appname, hostname, line):
    read, write = os.pipe()
    os.write(write, line)
    os.close(write)

    procid = "gigalixir-run"
    log_shuttle_cmd = "/opt/gigalixir/bin/log-shuttle -logs-url=http://token:%s@post.logs.gigalixir.com/logs -appname %s -hostname %s -procid %s" % (logplex_token, appname, hostname, procid)
    subprocess.check_call(log_shuttle_cmd.split(), stdin=read)

def download_file(url, local_filename):
    # NOTE the stream=True parameter
    r = requests.get(url, stream=True)
    with open(local_filename, 'wb') as f:
        for chunk in r.iter_content(chunk_size=1024): 
            if chunk: # filter out keep-alive new chunks
                f.write(chunk)
                #f.flush() commented by recommendation from J.F.Sebastian
    return local_filename

@contextlib.contextmanager
def cd(newdir, cleanup=lambda: True):
    prevdir = os.getcwd()
    os.chdir(os.path.expanduser(newdir))
    try:
        yield
    finally:
        os.chdir(prevdir)
        cleanup()

def find(name, path):
    for root, dirs, files in os.walk(path):
        if name in files:
            return os.path.join(root, name)
