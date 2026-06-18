"""
Script with helper functions to get setup

"""
import logging
from datetime import datetime
from pathlib import Path
import tomllib



def init_config() -> dict:
    # Checks config file setup & converts args to expected dtype
    # Raises errors if missing required args 
    target_dtypes: dict = {
        'path_to_csv': Path,
        'log-dir': Path,
        'log-to-file': bool,
        'log-level': str,
    }

    with open('./config.toml', 'rb') as f:
        config = tomllib.load(f)
    
    # Convert args to expected dtype
    for key, val in config.items():
        try:
            config[key] = target_dtypes[key](val)
        except (ValueError, TypeError):
            # Skip or handle conversion errors
            print(f"Warning: Could not convert key '{key}' with value '{val}' to {target_dtypes[key].__name__}")
    return config

def init_logger(config: dict) -> None:
    """
    Init. logger based on config file
    """
    if config['log-to-file']:
        config['log-dir'].mkdir(exist_ok=True)
        log_filename = config['log-dir'] / datetime.now().strftime("logfile_%Y-%m-%d_%H-%M-%S.log")

    log_level = config['log-level']
    level = getattr(logging, log_level.upper(), logging.INFO)

    logging.basicConfig(
        filename=log_filename if config["log-to-file"] else None,
        level=level,
        format="[%(asctime)s] [%(levelname)s] (%(name)s:%(lineno)d) - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
        )
    logging.getLogger('httpx').setLevel(logging.WARNING)
    return logging.getLogger(__name__)