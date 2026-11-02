"""The cluster command the agents run, mounted at /opt/cluster in each ops container.

    python3 /opt/cluster status
    python3 /opt/cluster take_down db-1     (also upgrade, bring_up, restart)

It only calls the service's API: the event log and the quorum check live in the service.
"""

import sys
import time
import urllib.error
import urllib.request

API = "http://cluster:8000"


def call(method: str, path: str) -> tuple[int, str]:
    request = urllib.request.Request(API + path, method=method)
    for _ in range(20):
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()
        except urllib.error.URLError:
            # the service may still be starting
            time.sleep(0.5)
    sys.exit("error: the cluster service is not answering")


if len(sys.argv) == 2 and sys.argv[1] == "status":
    status, text = call("GET", "/status")
elif len(sys.argv) == 3 and sys.argv[1] in (
    "take_down",
    "upgrade",
    "bring_up",
    "restart",
):
    status, text = call("POST", f"/{sys.argv[1]}/{sys.argv[2]}")
else:
    sys.exit(__doc__)
print(text)
sys.exit(0 if status == 200 else 1)
