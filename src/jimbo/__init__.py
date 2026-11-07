from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jimbo-agent")  # set in pyproject.toml; this reads it from the installed package
except PackageNotFoundError:  # run from source without installing
    __version__ = "dev"
