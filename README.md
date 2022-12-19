# kChat backend for Errbot

This is the kChat backend for errbot.

1. Create a virtual environment for errbot.

```shell
python3 -m venv <path_to_virtualenv>
source <path_to_virtualenv>/bin/activate
```

2. Install errbot

```shell
pip install errbot kchatdriver
```

3. Initialise errbot and configure mattermost.

```shell
errbot --init
```

4. Install err-backend-kchat

```shell
git clone https://github.com/infomaniak/errbot-kchat-backend.git backends/errbot-kchat-backend
```

5. Edit config.py

```python
import logging
import os

STORAGE = "Memory"
PLUGINS_CALLBACK_ORDER = (None,)

local_dir_path = os.path.dirname(__file__)
BOT_DATA_DIR = os.path.join(local_dir_path, "data")
BOT_EXTRA_PLUGIN_DIR = os.path.join(local_dir_path, "plugins")
BOT_EXTRA_BACKEND_DIR = os.path.join(local_dir_path, "backends")
BOT_LOG_LEVEL = logging.DEBUG

BACKEND = "kChat"

BOT_LOG_FILE = os.path.join(local_dir_path, "errbot.log")

BOT_ADMINS = (
    "@CHANGE_ME",
)

BOT_IDENTITY = {
    # Required
    "team": "CHANGE_ME",
    "server": "CHANGE_ME.kchat.infomaniak.com",
    "websocket_url": "websocket.kchat.infomaniak.com",
    "token": "CHANGE_ME"
}
```

Replace the BOT_ADMINS by your username.

In BOT_IDENTITY replace the team name by the subdomain url of your kchat, and the server field with your kChat server url.

Token will be given when creating a Bot in the integrations page of your kChat.

Some kChat actions can only be performed with administrator rights. If the bot has problems performing an action, check the bot account permissions and grant the appropriate rights.