#!/usr/bin/env python

####################
# Required Modules #
####################

# from gevent import monkey
# monkey.patch_all()

# Generic/Built-in
import argparse
import logging
import os
import time
import uuid
from multiprocessing import Process
from typing import Dict, List, Callable, Any

# Libs
import ray

# Custom
import config
from config import (
    SRC_DIR, RETRY_INTERVAL,
    capture_system_snapshot,
    configure_node_logger, 
    configure_sysmetric_logger
)
from synmanager.completed_operations import CompletedConsumerOperator

##################
# Configurations #
##################

SOURCE_FILE = os.path.abspath(__file__)

SECRET_KEY = "synergos_director" #os.urandom(24) # secret key

#############
# Functions #
#############

def construct_queue_kwargs(**kwargs):
    """ Extracts queue configuration values for linking server to said queue

    Args:
        kwargs: Any user input captured 
    Returns:
        Queue configurations (dict)
    """
    queue_config = kwargs['queue']

    queue_variant = queue_config[0]
    if queue_variant not in ["rabbitmq"]:
        raise argparse.ArgumentTypeError(
            f"Specified queue variant '{queue_variant}' is not supported!"
        )

    server = queue_config[1]
    port = int(queue_config[2])

    return {'host': server, 'port': port}


def construct_logger_kwargs(**kwargs) -> dict:
    """ Extracts user-parsed values and re-mapping them into parameters 
        corresponding to those required of components from Synergos Logger.

    Args:
        kwargs: Any user input captured 
    Returns:
        Logger configurations (dict)
    """
    logger_name = kwargs['id']

    logging_config = kwargs['logging_variant']

    logging_variant = logging_config[0]
    if logging_variant not in ["basic", "graylog"]:
        raise argparse.ArgumentTypeError(
            f"Specified variant '{logging_variant}' is not supported!"
        )

    server = (logging_config[1] if len(logging_config) > 1 else None)
    port = (int(logging_config[2]) if len(logging_config) > 1 else None)

    debug_mode = kwargs['debug']
    logging_level = logging.DEBUG if debug_mode else logging.INFO
    debugging_fields = debug_mode

    is_censored = kwargs['censored']
    censor_keys = (
        [
            'SRC_DIR', 'IN_DIR', 'OUT_DIR', 'DATA_DIR', 'MODEL_DIR', 
            'CUSTOM_DIR', 'TEST_DIR', 'DB_PATH', 'CACHE_TEMPLATE', 
            'PREDICT_TEMPLATE'
        ]
        if is_censored 
        else []
    )

    return {
        'logger_name': logger_name,
        'logging_variant': logging_variant,
        'server': server,
        'port': port,
        'logging_level': logging_level,
        'debugging_fields': debugging_fields,
        'censor_keys': censor_keys
    }


def archive_cycle(
    host: str,
    port: int,
    align_ops: Callable,
    train_ops: Callable,
    optim_ops: Callable,
    valid_ops: Callable,
    **kwargs
):
    """ Run endless loop to poll messages across different queues and run 
        respective callback operations. The hierachy of priority between queues, 
        starting from the highest is as follows: 
            Preprocess -> Train -> Evaluate (Validation or Prediction)

    Args:
        host (str): IP of server where queue is hosted on
        port (int): Port of server allocated to queue
        preprocess_job (Callable): Function to execute when handling a polled
            preprocessing job from the "Preprocess" queue
        train_job (Callable): Function to execute when handling a polled
            training job from the "Train" queue
        validate_job (Callable): Function to execute when handling a polled
            validation job from the "Evaluate" queue
        predict_job (Callable): Function to execute when handling a polled
            prediction job from the "Evaluate" queue
    """
    def archival_ops(
        process: str, 
        filters: List[str],
        outputs: Dict[str, Any]
    ) -> Callable:
        """
        
        Args:
            process (str):
            filters (list(str)):
            outputs (dict):
        """
        OPS_MAPPINGS = {
            'preprocess': align_ops,
            'train': train_ops,
            'optimize': optim_ops,
            'validate': valid_ops
        }

        selected_ops = OPS_MAPPINGS[process]
        return selected_ops(filters, outputs)

    logger = kwargs.get('logger', logging)

    completed_consumer = CompletedConsumerOperator(host=host, port=port)

    try:
        ###########################
        # Implementation Footnote #
        ###########################

        # [Cause]
        # When there are multiple grids involved, there is the capacity for
        # the Synergos grid to write results concurrently to "database.json"

        # [Problems]
        # "database.json" is managed by TinyDB, which does not scale well in
        # terms of concurrency. 

        # [Solution]
        # Future versions may see database substitution. For now, use queue to
        # linearize archival processes. All jobs have been split into 2 
        # processes - functional and archival. Functional processes will be 
        # executed in TTP(s), while archival processes will be sent as messages
        # into the completed queue, where the director will be the only 
        # component writing to "database.json".  

        completed_consumer.connect()

        while True:

            # try:
            completed_messages = completed_consumer.check_message_count()

            if completed_messages > 0:
                completed_consumer.poll_message(archival_ops)

            else:
                logger.synlog.info(
                    f"No archival operations in queue! Waiting for {RETRY_INTERVAL} second...",
                    ID_path=os.path.join(SRC_DIR, "config.py"), 
                    ID_function=archive_cycle.__name__
                )
         
            # except Exception as e:
            #     logger.synlog.error(
            #         f"Something went wrong while running a job! Error: {e}",
            #         ID_path=os.path.join(SRC_DIR, "config.py"), 
            #         ID_function=poll_cycle.__name__
            #     )

            time.sleep(RETRY_INTERVAL)
    
    except KeyboardInterrupt:
        logger.synlog.info(
            "[Ctrl-C] recieved! Archival processes terminated.",
            ID_path=os.path.join(SRC_DIR, "config.py"), 
            ID_function=archive_cycle.__name__
        )

    finally:
        completed_consumer.disconnect()

###########
# Scripts #
###########

if __name__ == "__main__":
    
    parser = argparse.ArgumentParser(
        description="REST-RPC Orchestrator for a Synergos Network."
    )

    parser.add_argument(
        "--id",
        "-i",
        type=str,
        default=f"director/{uuid.uuid4()}",
        help="ID of orchestrating party. e.g. --id TTP"
    )

    parser.add_argument(
        "--queue",
        "-mq",
        type=str,
        default=["rabbitmq"],
        nargs="+",
        help="Type of queue framework to use. eg. --queue rabbitmq 127.0.0.1 5672"
    )

    parser.add_argument(
        "--logging_variant",
        "-l",
        type=str,
        default="basic",
        nargs="+",
        help="Type of logging framework to use. eg. --logging_variant graylog 127.0.0.1 9400"
    )

    parser.add_argument(
        '--censored',
        "-c",
        action='store_true',
        default=False,
        help="Toggles censorship of potentially sensitive information on this orchestrator node"
    )

    parser.add_argument(
        '--debug',
        "-d",
        action='store_true',
        default=False,
        help="Toggles debugging mode on this orchestrator node"
    )

    input_kwargs = vars(parser.parse_args())

    # Set up core logger
    server_id = input_kwargs['id']
    logger_kwargs = construct_logger_kwargs(**input_kwargs)
    node_logger = configure_node_logger(**logger_kwargs)
    node_logger.synlog.info(
        f"Orchestrator `{server_id}` -> Snapshot of Input Parameters",
        **input_kwargs
    )
    node_logger.synlog.info(
        f"Orchestrator `{server_id}` -> Snapshot of Logging Parameters",
        **logger_kwargs
    )

    system_kwargs = capture_system_snapshot()
    node_logger.synlog.info(
        f"Orchestrator `{server_id}` -> Snapshot of System Parameters",
        **system_kwargs
    )

    # Bind node to queue
    mq_kwargs = construct_queue_kwargs(**input_kwargs)
    node_logger.synlog.info(
        f"Orchestrator `{server_id}` -> Snapshot of Queue Parameters",
        **mq_kwargs
    )

    # Set up sysmetric logger
    sysmetric_logger = configure_sysmetric_logger(**logger_kwargs)
    sysmetric_logger.track(
        file_path=SOURCE_FILE,
        class_name="",
        function_name=""        
    )

    ###########################
    # Implementation Footnote #
    ###########################

    # [Cause]
    # To allow custom Synergos Logging components to permeate the entire
    # system, these loggers have to be initialised first before the system
    # performs module loading. 

    # [Problems]
    # Importing app right at the start of the page causes system modules to
    # be loaded first, resulting in AttributeErrors, since 
    # synlogger.general.WorkerLogger has not been intialised, and thus, its
    # corresponding `synlog` attribute cannot be referenced.

    # [Solution]
    # Import system modules only after loggers have been intialised.

    from rest_rpc import initialize_app
    
    from rest_rpc.training.alignments import archive_alignment_outputs
    from rest_rpc.training.models import archive_training_outputs
    from rest_rpc.training.optimizations import archive_optimization_outputs
    from rest_rpc.evaluation.validations import archive_validation_outputs
        
    # Initialize subprocess to pull from completed queue
    archival_process = Process(
        target=archive_cycle, 
        kwargs={
            **mq_kwargs,
            'align_ops': archive_alignment_outputs,
            'train_ops': archive_training_outputs,
            'optim_ops': archive_optimization_outputs,
            'valid_ops': archive_validation_outputs,
            'logger': node_logger
        }
    )

    try:
        # Start background archival process
        archival_process.start()
        
        # Start main REST server
        app = initialize_app(settings=config)
        app.run(host="0.0.0.0", port=5000)

    finally:
        # Stop systemetic logging
        sysmetric_logger.terminate()
        
        # Stop archival processes
        archival_process.terminate()
        archival_process.join()
        archival_process.close


