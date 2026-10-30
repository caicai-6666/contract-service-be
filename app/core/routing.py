"""统一应用路径，供路由挂载、接口文档及返回的资源地址共用。"""

import os

APPLICATION_PREFIX = f'{os.environ.get("APP_PATH_PREFIX", "")}/knowledge-accumulation'
API_PREFIX = f'{APPLICATION_PREFIX}/contract/api'
