#! /usr/bin/python2

import argparse
import time
from multiprocessing import active_children
from dst.libraries.multiplier import multiplier
from multiprocessing import Process, Queue
from dst.output import console
from dst.worker import worker
import dst.dispatchers
from threading import Thread
import dst.listeners
from config import *
from argparse import ArgumentTypeError as err
import os

class PathType(object):
    def __init__(self, exists=True, type='file', dash_ok=True):
        '''exists:
                True: a path that does exist
                False: a path that does not exist, in a valid parent directory
                None: don't care
           type: file, dir, symlink, None, or a function returning True for valid paths
                None: don't care
           dash_ok: whether to allow "-" as stdin/stdout'''

        assert exists in (True, False, None)
        assert type in ('file','dir','symlink',None) or hasattr(type,'__call__')

        self._exists = exists
        self._type = type
        self._dash_ok = dash_ok

    def __call__(self, string):
        if string=='-':
            # the special argument "-" means sys.std{in,out}
            if self._type == 'dir':
                raise err('standard input/output (-) not allowed as directory path')
            elif self._type == 'symlink':
                raise err('standard input/output (-) not allowed as symlink path')
            elif not self._dash_ok:
                raise err('standard input/output (-) not allowed')
        else:
            e = os.path.exists(string)
            if self._exists==True:
                if not e:
                    raise err("path does not exist: '%s'" % string)

                if self._type is None:
                    pass
                elif self._type=='file':
                    if not os.path.isfile(string):
                        raise err("path is not a file: '%s'" % string)
                elif self._type=='symlink':
                    if not os.path.symlink(string):
                        raise err("path is not a symlink: '%s'" % string)
                elif self._type=='dir':
                    if not os.path.isdir(string):
                        raise err("path is not a directory: '%s'" % string)
                elif not self._type(string):
                    raise err("path not valid: '%s'" % string)
            else:
                if self._exists==False and e:
                    raise err("path exists: '%s'" % string)

                p = os.path.dirname(os.path.normpath(string)) or '.'
                if not os.path.isdir(p):
                    raise err("parent path is not a directory: '%s'" % p)
                elif not os.path.exists(p):
                    raise err("parent directory does not exist: '%s'" % p)

        return string

if __name__ == "__main__":

    #
    # Argument parsing
    #
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='Runs S&T attack on specified target, using trained pipeline generated by generate_model.py',
        epilog='''\

    S&T works with chains of operators - building blocks that pass data forward.
    Pre-made chains, called opmodes, can be selected with the syntax:

        %(prog)s --opmode MODE --target TARGET --pipeline PIPELINE

    Currently available chains are the following:
        - from_file loads specified wavfile and attacks it with specified pipeline

    Chains can also be specified block by block by setting the respective parameters.
    Data flow between blocks is the following:

        LISTENER --> DISPATCHER --> PIPELINE --> OUTPUT

    where
        - LISTENER is a function that loads audio. Right now only wavfile reader is provided.
        - DISPATCHER is a function that extracts keypress sounds. Right now only an offline dispatcher, that works on
                        a complete audio file (i.e., not a stream) is provided.
        - PIPELINE is a file with a pickled, trained Sklearn pipeline performing feature extraction and classification.
        - OUTPUT shows attack results. Right now only screen output, that prints results on terminal, is provided.
        '''
    )
    # Misc arguments such as version and help
    parser.add_argument('--version', '-v', action='version', version=CONFIG.VERSION)
    # Opmode is a convenience to avoid specifying operator chains
    parser.add_argument("--opmode", choices=['from_file', ],
                        help='Convenience syntax to avoid specifying operator chains')
    parser.add_argument("--target", "-t", type=str,
                        help='Attack target. Valid values depend on the listener')
    # If no opmode is used you can specify operator chains with safe defaults
    parser.add_argument("--listener", "-l", choices=['wavfile', 'input', 'input_interactive'])
    parser.add_argument("--dispatcher", "-d", choices=['offline'])
    # Define the sklearn pipeline to use
    # Multiple pipelines will be allowed - each will receive from dispatcher
    # Watch out - right now only a SINGLE pipeline works
    parser.add_argument("--pipeline", "-p", action='append', type=PathType(exists=True, type='dir'), required=True,
                        help='Trained pipeline created by generate_model.py')
    # General options
    parser.add_argument("--workers", "-w", type=int, default=CONFIG.workers,
                        help='Number of workers to dispatch')
    parser.add_argument("--dispatcher_window_size", type=int, default=CONFIG.dispatcher_window_size,
                        help='Window size of keypress samples, in milliseconds')
    parser.add_argument("--dispatcher_threshold", type=int, default=CONFIG.dispatcher_threshold,
                        help='Percentile threshold of keypress sound vs. background noise, [0, 100]')
    parser.add_argument("--dispatcher_min_interval", type=int, default=CONFIG.dispatcher_min_interval,
                        help='Minimum interval between keystrokes, in milliseconds')
    parser.add_argument("--dispatcher_step_size", type=int, default=CONFIG.dispatcher_step_size,
                        help='Scan granularity of dispatchers, in milliseconds')
    parser.add_argument("--dispatcher_persistence", type=int, default=CONFIG.dispatcher_persistence,
                        help='Whether to save mined events')
    parser.add_argument("--n_predictions", "-n", type=int, default=10,
                        help='Number of required predictions for each sample')

    args = parser.parse_args()

    #
    # Configuration - update values
    #
    for key in vars(CONFIG).keys():
        if key in vars(args).keys():
            CONFIG.key = args[key]
    for key, val in vars(args).items():
        CONFIG.key = val

    #
    # Main matter
    #
    # Chain elements registration lists
    pipeline_list = []
    # Multipliers and outputs need to be stopped separately
    output_list = []
    multiplier_list = []

    # Convert opmodes to chains first
    if args.opmode == 'from_file':
        args.listener, args.dispatcher = 'wavfile', 'offline'

    # For each chain part, import modules and register them to registration lists
    # First init the listener, remember its output queue
    lq = Queue()
    p = Process(target=getattr(dst.listeners, args.listener), args=(args.target, lq, CONFIG))
    p.daemon = True
    p.start()

    # Create the required dispatcher
    oq, dq = Queue(), Queue()
    p = Process(target=getattr(dst.dispatchers, args.dispatcher), args=(lq, oq, dq, CONFIG))
    p.daemon = True
    p.start()
    # For each pipeline, create a pool of workers
    for p_idx, pipeline in enumerate(args.pipeline):
        iq, rq = Queue(), Queue()
        for n_worker in range(args.workers):
            p = Process(target=worker, args=(pipeline, iq, rq, args.n_predictions, CONFIG))
            p.daemon = True
            p.start()
        pipeline_list.append(iq)
        # Send the output of the pipeline to a terminal, to be displayed
        p = Thread(target=console, args=(rq, CONFIG))
        p.daemon = True
        p.start()
        output_list.append((p, rq))
    # Clone dispatcher output to each pipeline input
    p = Process(target=multiplier, args=(oq, [_q for _q in pipeline_list]))
    multiplier_list.append((p, oq))
    p.daemon = True
    p.start()

    #
    # Exit: wait until everyone (except multipliers and outputs who cannot join()
    #
    while len(active_children()) > len(output_list) + len(multiplier_list):
        time.sleep(1)
        pass
    # Wait for user action to join output and terminate
    for _mulp in multiplier_list:
        _mulp[1].put(None)
    for _outp in output_list:
        _outp[1].put(None)
        _outp[0].join()