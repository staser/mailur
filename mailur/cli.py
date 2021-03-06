"""Mailur CLI

Usage:
  mlr remote <login> set <username> <password> --imap=<host> --smtp=<host>
  mlr remote <login> [--tag=<tag> --box=<box> --parse] [options]
  mlr parse <login> [<criteria>] [options]
  mlr metadata <login> [<uids>]
  mlr sync <login> [--timeout=<timeout>]
  mlr sync-flags <login> [--reverse]
  mlr clean-flags <login> <flag>...
  mlr icons
  mlr web
  mlr lint [--ci]
  mlr test
  mlr -h | --help
  mlr --version

Options:
  -b <batch>    Batch size [default: 1000].
  -t <threads>  Amount of threads for thread pool [default: 2].
"""
import functools as ft
import pathlib
import sys
import time

from docopt import docopt
from gevent import joinall, sleep, spawn

from . import conf, imap, local, lock, log, remote

root = pathlib.Path(__file__).resolve().parent.parent


def main(args=None):
    if args is None:
        args = sys.argv[1:]
    args = docopt(__doc__, args, version='Mailur 0.3')
    try:
        process(args)
    except KeyboardInterrupt:
        raise SystemExit('^C')


def process(args):
    conf['USER'] = args['<login>']
    opts = {
        'batch': int(args.get('-b')),
        'threads': int(args.get('-t')),
    }
    if args['remote'] and args['set']:
        remote.data_account({
            'username': args['<username>'],
            'password': args['<password>'],
            'imap_host': args['--imap'],
            'smtp_host': args['--smtp'],
        })
    elif args['remote']:
        select_opts = dict(tag=args['--tag'], box=args['--box'])
        fetch_opts = dict(opts, **select_opts)
        fetch_opts = {k: v for k, v in fetch_opts.items() if v}

        remote.fetch(**fetch_opts)
        if args['--parse']:
            local.parse(**opts)
    elif args['parse']:
        local.parse(args.get('<criteria>'), **opts)
    elif args['sync']:
        sync(int(args['--timeout']))
    elif args['sync-flags']:
        if args['--reverse']:
            local.sync_flags_to_src()
        else:
            local.sync_flags_to_all()
    elif args['clean-flags']:
        local.clean_flags(args['<flag>'])
    elif args['metadata']:
        local.update_metadata(args.get('<uids>'))
    elif args['icons']:
        icons()
    elif args['web']:
        web()
    elif args['test']:
        run('pytest -q -n2 --cov=mailur --cov-report=term-missing')
    elif args['lint']:
        ci = args['--ci'] and 1 or ''
        run('ci=%s bin/run-lint' % ci)
    else:
        raise SystemExit('Target not defined:\n%s' % args)


def run_forever(fn):
    # but if it always raises exception, run only 3 times

    @ft.wraps(fn)
    def inner(*a, **kw):
        count = 3
        while count:
            try:
                fn(*a, **kw)
            except Exception as e:
                log.exception(e)
                sleep(10)
                count = -1
    return inner


def sync(timeout=1200):
    def sync_remote(res=None):
        imap.clean_pool()
        try:
            remote.fetch()
            local.parse()
        except lock.Error as e:
            log.warn(e)

    @run_forever
    def idle_remote(params):
        with remote.client(**params) as c:
            c.idle(sync_remote, timeout=timeout)

    @run_forever
    def sync_flags():
        imap.clean_pool()
        local.sync_flags_to_all()
        local.sync_flags(timeout=timeout)

    try:
        sync_remote()
        jobs = [spawn(sync_flags)]
        for params in remote.get_folders():
            jobs.append(spawn(idle_remote, params))
        joinall(jobs, raise_error=True)
    except KeyboardInterrupt:
        time.sleep(1)


def web():
    from gevent.subprocess import run
    from gevent.pool import Pool

    def api():
        run('bin/run-web', shell=True)

    def webpack():
        run('command -v yarn && yarn run dev || npm run dev', shell=True)

    try:
        pool = Pool()
        pool.spawn(api)
        pool.spawn(webpack)
        pool.join()
    except KeyboardInterrupt:
        time.sleep(1)


def run(cmd):
    from sys import exit
    from subprocess import call

    check = 'command -v pytest'
    if call(check, cwd=root, shell=True):
        raise SystemExit(
            'Test dependencies must be installed.\n'
            '$ pip install -e .[test]'
        )

    cmd = 'sh -xc %r' % cmd
    exit(call(cmd, cwd=root, shell=True))


def icons():
    import json
    import bottle

    font = root / 'assets/font'
    sel = (font / 'selection.json').read_text()
    sel = json.loads(sel)
    sel_pretty = json.dumps(sel, indent=2, ensure_ascii=False, sort_keys=True)
    (font / 'selection.json').write_text(sel_pretty)
    icons = [
        (i['properties']['name'], '\\%s' % hex(i['properties']['code'])[2:])
        for i in sel['icons']
    ]
    tpl = str((font / 'icons.less.tpl').resolve())
    txt = bottle.template(
        tpl, icons=icons,
        template_settings={'syntax': '{% %} % {{ }}'}
    )
    f = font / 'icons.less'
    f.write_text(txt)
    print('%s updated' % f)


if __name__ == '__main__':
    main()
