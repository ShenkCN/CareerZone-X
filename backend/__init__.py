# 包标记：让 Python 把 backend 当成可导入的包。
# 这里不放业务逻辑，避免 import backend 时产生副作用（例如提前连数据库）。
__version__ = "0.4.0"
