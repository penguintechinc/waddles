import inspect
import os
from botClasses import *
from botLogger import botLogger

# Why is that black van out there?
log = botLogger("waddlebot-dbc")
log.fileLogger("waddlebot-dbc.log")

abs_path = os.path.abspath((inspect.stack()[0])[1])
directory_of_1py = os.path.dirname(abs_path)

# ---------------------
# Dataclass for botconfig
# ---------------------
class botConfig:
    def __init__(self, configType: str = "yaml", configPath: str = "config.yml"):
        self.cpath = configPath
        match configType:
            case "yaml":
                self.config = self.__importyaml()
                log.debug("Config type is yaml")
            case _:
                self.config = self.__importyaml()

    # ---------------------
    # This is a helper function will import the config file in yaml format
    # ---------------------
    def __importyaml(self):
        from yaml import safe_load
        try:
            with open(self.cpath, 'r') as thefile:
                log.debug(f"Importing {self.cpath}")
                c = safe_load(thefile)
                thefile.close
        except Exception as err:
            log.error(err)
        return c
