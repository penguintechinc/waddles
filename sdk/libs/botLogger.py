import logging
import inspect


# ---------------------
# This is a class which will handle all logging for the bot
# ---------------------
class BotLogger:
    def __init__(self, logname: str = "waddlebot"):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        self.callFunction = None

    # ---------------------
    # This is a function which will set the handler name to the caller function
    # ---------------------
    def caller(self):
        try:
            self.callFunction = inspect.stack()[2][3]
        except Exception:
            self.logger.debug("Unable to get the caller function 2 levels up, tryin 1 level up!")
        if len(self.callFunction) < 2:
            self.callFunction = inspect.stack()[1][3]

    # ---------------------
    # This is a function which will create a logger using file handler
    # ---------------------
    def fileLogger(self, file):
        file_handler = logging.FileHandler(file)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        self.logger.addHandler(file_handler)

    # ---------------------
    # This is a function which will create a logger using syslog handler
    # ---------------------
    def syslogLogger(self):
        syslog_handler = logging.handlers.SysLogHandler(address='/dev/log')
        syslog_handler.setLevel(logging.INFO)
        syslog_handler.setFormatter(logging.Formatter('%(name)s - %(levelname)s - %(message)s'))
        self.logger.addHandler(syslog_handler)

    # ---------------------
    # This is a function which will create a logger using file handler with JSON format
    # ---------------------
    def fileJSONLogger(self):
        json_handler = logging.FileHandler('log.json')
        json_handler.setLevel(logging.INFO)
        json_handler.setFormatter(logging.Formatter('{"function": "%(name)s", "level": "%(levelname)s", "rawMsg": "%(message)s"}'))
        self.logger.addHandler(json_handler)
