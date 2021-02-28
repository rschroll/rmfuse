# Copyright 2021 Robert Schroll
# This file is part of RMfuse and is distributed under the MIT license.

import configparser
import inspect
import logging
import sys

from xdg import xdg_config_home

from rmrl import render

log = logging.getLogger(__name__)
CONFIG_FILE = xdg_config_home() / 'rmfuse' / 'config.ini'

def _get_render_defaults():
    defaults = inspect.getfullargspec(render).kwonlydefaults
    del defaults['progress_cb']
    return defaults

DEFAULTS = {
    'render': _get_render_defaults()
}

LOOKUP_FUNCS = {
    bool:  'getboolean',
    float: 'getfloat',
    int:   'getint',
    str:   'get'
}

def to_dict(config, defaults):
    res = {}
    for k, v in defaults.items():
        if isinstance(v, dict):
            res[k] = to_dict(config[k], defaults[k])
        else:
            try:
                res[k] = getattr(config, LOOKUP_FUNCS[type(defaults[k])])(k)
            except (TypeError, ValueError, KeyError):
                log.warning(f'Failed to read "{k}: {config[k]}" as {type(defaults[k])}')
                res[k] = defaults[k]
    return res

def get_config(section=None):
    config = configparser.ConfigParser()
    config.read_dict(DEFAULTS)
    if CONFIG_FILE.is_file():
        config.read_file(CONFIG_FILE.open('r'))
    if section:
        return to_dict(config[section], DEFAULTS[section])
    return to_dict(config, DEFAULTS)

def write_default_config():
    config = configparser.ConfigParser()
    config.read_dict(DEFAULTS)
    if CONFIG_FILE.exists():
        log.warning(f'{CONFIG_FILE} already exists; output to stdout intead')
        fout = sys.stdout
        do_close = False
        fout.write('\n')
    else:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        print(f'Writing config to {CONFIG_FILE}')
        fout = CONFIG_FILE.open('w')
        do_close = True
    config.write(fout)
    if do_close:
        fout.close()
