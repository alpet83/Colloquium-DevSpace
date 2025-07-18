from basic_logger import BasicLogger
logger = BasicLogger("test", "test_", None)
logger.debug("Debug message %s", "test")
logger.info("Info message %d", 42)
logger.warn("Warning message %s", "alert")
logger.error("Error message %s", "fail")
logger.excpt("Exception message", exc_info=(Exception, Exception("test"), None))