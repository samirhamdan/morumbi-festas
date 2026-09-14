import os

bind = os.environ.get("FESTAS_BIND", "172.18.0.1") + ":" + os.environ.get("FESTAS_PORTA", "5001")
workers = int(os.environ.get("FESTAS_WORKERS", "1"))
timeout = 120
max_requests = 200
max_requests_jitter = 30
