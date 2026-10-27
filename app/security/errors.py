class SecurityError(Exception):
    def __init__(self, code: str, status_code: int = 403):
        self.code = code
        self.status_code = status_code
        super().__init__(code)
