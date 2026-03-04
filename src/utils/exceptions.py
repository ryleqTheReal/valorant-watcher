"""Contains all custom exceptions"""

CRITICAL_APP_ERROR = "CRITICAL_APP_ERROR"

class AppError(Exception):
    """Highest exception in hierarchy from which all other exceptions stem     

    Args:
        Exception (_type_): Thrown when everything went wrong
    """
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.is_critical: bool = True
        self.message: str = "Something went horribly wrong and the app crashed"
        self.internal_status: str = CRITICAL_APP_ERROR
        
    
    