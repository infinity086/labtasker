import re
from functools import wraps
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from pydantic import ValidationError, validate_call
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_500_INTERNAL_SERVER_ERROR

from labtasker.utils import flatten_dict, validate_required_fields


def validate_arg(func):
    """Wrap around Pydantic `validate_call` to yield HTTP_400_BAD_REQUEST"""

    @wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return validate_call(func)(*args, **kwargs)
        except ValidationError as e:
            # Catch Pydantic validation errors and re-raise them as HTTP 400
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=e.errors(),  # Provide detailed validation errors
            ) from e
        except HTTPException:
            # Allow pre-existing HTTPExceptions to propagate
            raise
        except Exception as e:
            # Catch any other exception and raise it as a generic HTTP 500 error
            raise HTTPException(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e),
            ) from e

    return wrapped


def query_dict_to_mongo_filter(query_dict, parent_key=""):
    mongo_filter = {}

    flattened_query = flatten_dict(query_dict, parent_key=parent_key)
    for full_key in flattened_query.keys():
        mongo_filter[full_key] = {"$exists": True}

    return mongo_filter


def merge_filter(*filters, logical_op="and"):
    """
    Merge multiple MongoDB filters using a specified logical operator, while ignoring empty filters.

    Args:
        *filters: Arbitrary number of filter dictionaries to merge.
        logical_op (str): The logical operator to use for merging filters.
                          Must be one of "and", "or", or "nor".

    Returns:
        dict: A MongoDB query filter with the specified logical operator applied.

    Raises:
        HTTPException: If the logical_op is not valid.
    """
    if logical_op not in ["and", "or", "nor"]:
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Invalid logical operator: {logical_op}. Must be 'and', 'or', or 'nor'.",
        )

    valid_filters = [
        f for f in filters if f
    ]  # Filters out None, {}, or other falsy values

    # If no valid filters remain, return an empty filter
    if not valid_filters:
        return {}

    if len(valid_filters) == 1:
        return valid_filters[0]

    mongo_logical_op = f"${logical_op}"  # "$and", "$or", "$nor"

    return {mongo_logical_op: valid_filters}


def sanitize_query(queue_id: str, query: Dict[str, Any]) -> Dict[str, Any]:
    """Enforce only query on queue_id specified in query"""
    return {
        "$and": [
            {"queue_id": queue_id},  # Enforce queue_id
            query,  # Existing user query
        ]
    }


def arg_match(required, provided):
    """
    Check if all provided arguments are used in the required, in a top-to-down matching manner (check if provided is "covered" by "required").
    If the parent node is in the "required", then all sub-nodes in the "provided" are considered to be used.
    Additionally, everything in the required must be in the provided.
    Principle: No more, no less.
    """
    if required is None:  # Base case for recursion
        return True
    if provided is None:
        return False

    try:
        # Check if any required key is missing in provided (vice versa)
        if set(required.keys()) != set(provided.keys()):  # "No more, no less"
            return False
    except AttributeError:  # one of them is not dict
        return False

    # Recursively check each key and value pair
    for key, value in required.items():
        if not arg_match(value, provided[key]):
            return False

    return True


def keys_to_query_dict(keys: List[str], mode: str):
    """
    Converts a list of dot-separated keys into a nested dictionary
    (as a representation of the tree structure, where leaf node values are set to None.).


    Args:
        keys (list): List of strings, where each string is a dot-separated key path.
        mode (str): "deepest" or "topmost".
                    - "deepest": Fully expand all key paths and set the end as the leaf nodes.
                    - "topmost": Stop at the shortest key paths found in the list and set as leaf nodes.

    Returns:
        dict: Nested dictionary representation of the keys.
    """
    assert mode in ["deepest", "topmost"], "Mode must be 'deepest' or 'topmost'."

    validate_required_fields(keys)

    query_dict = {}
    keys = sorted(set(keys), key=len)  # Sort by length for topmost mode processing

    for key in keys:
        parts = key.split(".")  # Split the key into its parts
        current = query_dict

        # Check if a shorter path already exists for topmost mode
        if mode == "topmost":
            # Check if any prefix of this key already exists as a leaf node
            prefix_exists = False
            temp = query_dict
            # Traverse depth-wise
            for i, part in enumerate(parts):
                if part not in temp:
                    break
                if temp[part] is None:  # Found a leaf node
                    prefix_exists = True
                    break
                temp = temp[part]

            if prefix_exists:
                continue  # Skip this key as a shorter path already exists

        # Traverse depth-wise
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            elif current[part] is None:  # leaf node encountered
                if mode == "deepest":
                    current[part] = {}  # extend depth
                else:
                    break  # in topmost mode, don't extend past existing leaf

            current = current[part]  # Move to the next level

        # Only set the leaf node if we haven't encountered a break
        if current is not None:  # This check ensures we haven't broken out of the loop
            # Set the leaf node
            if (
                parts[-1] not in current
                or mode == "deepest"
                or current[parts[-1]] is not None
            ):
                current[parts[-1]] = None

    return query_dict


def sanitize_update(
    update: Dict[str, Any],
    banned_fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Ban update on certain fields."""

    if banned_fields is None:
        banned_fields = ["_id", "queue_id", "created_at", "last_modified"]

    def _recr_sanitize(d: Dict[str, Any]) -> Dict[str, Any]:
        for k, v in d.items():
            if k in banned_fields:
                raise HTTPException(
                    status_code=HTTP_400_BAD_REQUEST,
                    detail=f"Field {k} is not allowed to be updated",
                )
            elif isinstance(v, dict):
                d[k] = _recr_sanitize(v)
        return d

    return _recr_sanitize(update)


def sanitize_dict(dic: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize a dictionary so that it does not contain any MongoDB operators."""

    def _recr_sanitize(d: Dict[str, Any]) -> Dict[str, Any]:
        for k, v in d.items():
            if isinstance(k, str):
                if re.match(r"^\$", k):  # Match those starting with $
                    raise HTTPException(
                        status_code=HTTP_400_BAD_REQUEST,
                        detail=f"MongoDB operators are not allowed in field names: {k}",
                    )
                if k.startswith("."):
                    raise HTTPException(
                        status_code=HTTP_400_BAD_REQUEST,
                        detail=f"Field names starting with `.` are not allowed: {k}",
                    )
            if isinstance(v, dict):
                d[k] = _recr_sanitize(v)
        return d

    return _recr_sanitize(dic)


def auth_required(func):
    """
    A decorator to mark a function as requiring authentication.
    This does not enforce authentication but serves as a marker.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        # Just call the original function without enforcing anything
        return func(*args, **kwargs)

    # Add a marker attribute to the function
    wrapper.auth_required = True
    return wrapper
