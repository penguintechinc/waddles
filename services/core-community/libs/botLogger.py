import logging

# Usage Example:
# from botLogger import BotLogger       # This will import the BotLogger class
# mylog = BotLogger(logFile="bot.log")  # This will create a logger with the name waddlebot and logfile to bot.log
# mylog.fileLogger()                    # This will create an optional file handler for the logger, defaults to console
# mylog.info("This is a test message")  # This will log the message to the file through redirect
# log = mylog.logger                    # This will get the logger object directly
# log.info("This is a test message")    # This will log the message to the file directly

# ---------------------
# This is a class which will handle all logging for the bot
# ---------------------
class BotLogger:
    def __init__(self, logname: str = "waddlebot", logFile: str = "/var/log/waddlebot.log", 
                 logHost: str ="127.0.0.1:514", json = False) -> None:
        self.logger = logging.getLogger(logname)
        self.logger.setLevel(logging.INFO)
        self.callFunction = self.caller()
        self.logFile = logFile
        self.logHost = logHost
        self.jsonFormat = json
    
    # ---------------------
    # This is a function which will set the handler name to the caller function
    # ---------------------
    def caller(self) -> None:
        from inspect import stack
        try:
            self.callFunction = stack()[2][3]
        except Exception:
            self.logger.debug("Unable to get the caller 2 levels up, tryin 1 level up!")
        if len(self.callFunction) < 2:
            self.callFunction = stack()[1][3]
    
    # ---------------------
    # This is a function which will create a logger using file handler
    # ---------------------
    def fileLogger(self) -> None:
        file_handler = logging.FileHandler(self.logFile)
        file_handler.setLevel(logging.INFO)
        if self.jsonFormat:
            file_handler.setFormatter(
                logging.Formatter('{"function": "%(name)s", "level": "%(levelname)s", "rawMsg": "%(message)s"}'))
        else:
            file_handler.setFormatter(
                logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        self.logger.addHandler(file_handler)
    
    # ---------------------
    # This is a function which will create a logger using syslog handler
    # ---------------------
    def syslogLogger(self) -> None:
        syslog_handler = logging.handlers.SysLogHandler(address='/dev/log')
        syslog_handler.setLevel(logging.INFO)
        if self.jsonFormat:
            syslog_handler.setFormatter(
                logging.Formatter('{"function": "%(name)s", "level": "%(levelname)s", "rawMsg": "%(message)s"}'))
        else:
            syslog_handler.setFormatter(
                logging.Formatter('%(name)s - %(levelname)s - %(message)s'))
        self.logger.addHandler(syslog_handler)
    
    # ---------------------
    # This is a function which will create a logger using file handler with JSON format
    # ---------------------
    def fileJSONLogger(self) -> None:
        self.jsonFormat = True
        self.fileLogger()
    # ---------------------
    # this is a functin which will change the logging level
    # ---------------------
    def changeLevel(self, level) -> None:
        self.logger.setLevel(level)


    # ---------------------
    # Redirect call for info
    # ---------------------
    def info(self, msg: str) -> None:
        self.caller()
        self.logger.info(f"{self.callFunction} - {msg}")
        
    # ---------------------
    # Redirect call for error
    # ---------------------
    def error(self, msg: str) -> None:
        self.caller()
        self.logger.error(f"{self.callFunction} - {msg}")
        
    # ---------------------
    # Redirect call for debug
    # ---------------------
    def debug(self, msg: str):
        self.caller()
        self.logger.debug(f"{self.callFunction} - {msg}")
    
    # ---------------------
    # Redirect call for warning
    # ---------------------
    def warning(self, msg: str) -> None:
        self.caller()
        self.logger.warning(f"{self.callFunction} - {msg}")
    
    # ---------------------
    # Redirect call for critical
    # ---------------------
    def critical(self, msg: str) -> None:
        from sys import exit
        self.caller()
        self.logger.critical(f"{self.callFunction} - {msg}")
        exit((f"{self.callFunction} - {msg}", 1))
        
    # ---------------------
    # Redirect call for exception
    # ---------------------
    def exception(self, msg: str) -> None:
        self.caller()
        self.logger.exception(f"{self.callFunction} - {msg}")
        
    # ---------------------
    # Create a syslog handler with json format
    # ---------------------
    def syslogJSONLogger(self) -> None:
        self.jsonFormat = True
        self.syslogLogger()