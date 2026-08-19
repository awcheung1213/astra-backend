import logging


class Logger:
    # internal use only
    __instance = None
    logger = None

    def __new__(cls, name=None, level=logging.DEBUG, *args, **kwargs):
        cls.__instance = super().__new__(cls)
        cls.__instance.logger = logging.getLogger(name)
        cls.__instance.logger.setLevel(level)
        return cls.__instance

    def debug(self, message, priority=None):
        self.log(message, priority=priority, level=logging.DEBUG)

    def info(self, message, priority=None):
        self.log(message, priority=priority, level=logging.INFO)

    def error(self, message, priority=None):
        self.log(message, priority=priority, level=logging.ERROR)

    def warning(self, message, priority=None):
        self.log(message, priority=priority, level=logging.WARNING)

    def critical(self, message, priority=None):
        self.log(message, priority=priority, level=logging.CRITICAL)

    def log(self, message, priority=None, level=logging.DEBUG):
        if priority == 1:
            self.logger.log(level, f"########## {message} ########################################")
        elif priority == 2:
            self.logger.log(level, f"########## {message} ##########")
        elif priority == 3:
            self.logger.log(level, f"########## {message}")
        else:
            self.logger.log(level, message)


logger = Logger("astra-backend")
