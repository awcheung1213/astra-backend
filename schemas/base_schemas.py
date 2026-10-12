from datetime import datetime, timezone
from typing import Type, TypeVar

from pydantic import BaseModel as Response

from objects.base_object import BaseObject, Object_

Response_ = TypeVar("Response_", bound=Response)


class BaseSchema(BaseObject):
    pass


class BaseRequest(BaseSchema):
    pass


class BaseResponse(BaseSchema):
    class Config:
        nested_attribute: str

    @classmethod
    def create(cls: Type[Response_], data: dict | Object_ | list[Object_]) -> Response_:
        # create schema response from one of: dict, schema, list of a schema
        if isinstance(data, dict):
            # expect data to be not nested when a dict
            return cls(**data)
        elif isinstance(data, list):
            # expect data to be nested when a list of schemas
            return cls(
                **{
                    cls.Config.nested_attribute: [item for item in data],
                }
            )
        else:
            # expect data to be nested when a single schema
            return cls(
                **{
                    cls.Config.nested_attribute: data,
                }
            )


def utc_iso(moment: datetime | None) -> str | None:
    """ISO 8601 with an explicit UTC offset for API responses. Stored datetimes
    are naive UTC; without the offset browsers parse them as local time (BUG-044).
    Date-only fields (week_of, period, deadline) keep date.isoformat()."""
    if moment is None:
        return None
    return (moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)).isoformat()
