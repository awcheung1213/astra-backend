from typing import TypeVar

from pydantic import BaseModel as Object

Object_ = TypeVar("Object_", bound=Object)


class BaseObject(Object):
    pass
