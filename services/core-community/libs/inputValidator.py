from re import match
from botLogger import BotLogger

log = BotLogger()
log.fileLogger("validator.log")

# ---------------------
# This is a class which will handle all input validation for the bot
# ---------------------

class inputValidator:
    def __init__(self):
        self.valid = False
        log.info("Input Validator Initialized for %s", BotLogger.caller)

    def alphaNumeric(self, input: str):
        if match("^[a-zA-Z0-9]*$", input):
            self.valid = True
            log.debug("Valid AlphaNumeric")
        else:
            self.valid = False
            log.debug("Invalid AlphaNumeric")
        return self.valid
    def ipAddress(self, input: str):
        if match("^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", input):
            self.valid = True
            log.debug("Valid IP Address")
        else:
            self.valid = False
            log.debug("Invalid IP Address")
        return self.valid
    def email(self, input: str):
        if match("^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", input):
            self.valid = True
            log.debug("Valid Email")
        else:
            self.valid = False
            log.debug("Invalid Email")
        return self.valid
    def url(self, input: str):
        if match("^(http|https)://[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", input):
            self.valid = True
            log.debug("Valid URL")
        else:
            self.valid = False
            log.debug("Invalid URL")
        return self.valid
    def phone(self, input: str):
        if match("^\d{10}$", input):
            self.valid = True
            log.debug("Valid Phone Number")
        else:
            self.valid = False
            log.debug("Invalid Phone Number")
        return self.valid
    def zipCode(self, input: str):
        if match("^\d{5}$", input):
            self.valid = True
            log.debug("Valid Zip Code")
        else:
            self.valid = False
            log.debug("Invalid Zip Code")
        return self.valid
    def state(self, input: str):
        if match("^[A-Z]{2}$", input):
            self.valid = True
            log.debug("Valid State")
        else:
            self.valid = False
            log.debug("Invalid State")
        return self.valid
    def country(self, input: str):
        if match("^[A-Z]{2}$", input):
            self.valid = True
            log.debug("Valid Country")
        else:
            self.valid = False
            log.debug("Invalid Country")
        return self.valid
    def date(self, input: str):
        if match("^\d{4}-\d{2}-\d{2}$", input):
            self.valid = True
            log.debug("Valid Date")
        else:
            self.valid = False
            log.debug("Invalid Date")
        return self.valid
    def time(self, input: str):
        if match("^\d{2}:\d{2}:\d{2}$", input):
            self.valid = True
            log.debug("Valid Time")
        else:
            self.valid = False
            log.debug("Invalid Time")
        return self.valid
    def dateTime(self, input: str):
        if match("^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", input):
            self.valid = True
            log.debug("Valid DateTime")
        else:
            self.valid = False
            log.debug("Invalid DateTime")
        return self.valid
    def numericStr (self, input: str):
        if match("^\d*$", input):
            self.valid = True
            log.debug("Valid Numeric")
        else:
            self.valid = False
            log.debug("Invalid Numeric")
        return self.valid
    def alphaStr(self, input: str):
        if match("^[a-zA-Z]*$", input):
            self.valid = True
            log.debug("Valid Alpha")
        else:
            self.valid = False
            log.debug("Invalid Alpha")
        return self.valid
    def notEmpty(self, input: str):
        if match("^.+$", input):
            self.valid = True
            log.debug("Valid Not Empty")
        else:
            self.valid = False
            log.debug("Invalid Not Empty")
        return self.valid