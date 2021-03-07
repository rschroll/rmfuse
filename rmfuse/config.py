# Copyright 2021 Robert Schroll
# This file is part of RMfuse and is distributed under the MIT license.

import ast
from configparser import ConfigParser
import enum
import inspect
import logging
import sys

from xdg import xdg_config_home

from rmrl import render

log = logging.getLogger(__name__)
CONFIG_FILE = xdg_config_home() / 'rmfuse' / 'config.ini'

class FSMode(enum.Enum):
    meta = 'meta'
    raw = 'raw'
    orig = 'orig'
    annot = 'annot'

    def __str__(self):
        return self.name

def _get_render_defaults():
    defaults = inspect.getfullargspec(render).kwonlydefaults
    del defaults['progress_cb']
    return defaults

def get_literal(type_):
    def func(config, key):
        return type_(ast.literal_eval(config.get(key).strip()))
    return func

DEFAULTS = {
    'mount': {
        'mountpoint': '',
        'mode': FSMode.annot
    },
    'render': _get_render_defaults()
}

LOOKUP_FUNCS = {
    bool:   lambda config, key: config.getboolean(key),
    float:  lambda config, key: config.getfloat(key),
    int:    lambda config, key: config.getint(key),
    str:    lambda config, key: config.get(key),
    list:   get_literal(list),
    tuple:  get_literal(tuple),
    FSMode: lambda config, key: FSMode(config.get(key))
}

def to_dict(config, defaults):
    res = {}
    for k, v in defaults.items():
        if isinstance(v, dict):
            res[k] = to_dict(config[k], defaults[k])
        else:
            try:
                res[k] = LOOKUP_FUNCS[type(defaults[k])](config, k)
            except (TypeError, ValueError, KeyError):
                log.warning(f'Failed to read "{k}: {config[k]}" as {type(defaults[k])}')
                res[k] = defaults[k]
    return res

def get_config(section=None):
    config = ConfigParser()
    config.read_dict(DEFAULTS)
    if CONFIG_FILE.is_file():
        config.read_file(CONFIG_FILE.open('r'))
    if section:
        return to_dict(config[section], DEFAULTS[section])
    return to_dict(config, DEFAULTS)

def write_default_config():
    config = ConfigParser()
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
