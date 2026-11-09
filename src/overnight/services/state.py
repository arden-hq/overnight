from pydantic import ConfigDict

from overnight.domain.contracts import Contract, JsonObject


class State(Contract):
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False, validate_by_name=True
    )

    def json_object(self) -> JsonObject:
        return self.model_dump(mode="json", by_alias=True)

    def view_object(self) -> JsonObject:
        return self.model_dump(mode="json", by_alias=True, exclude_unset=True)


def distinct(ids: list[str], label: str) -> None:
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate {label} id")
