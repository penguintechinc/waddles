"""
Waddles Input Validation Library

Provides Pydantic-based validation for Flask/Quart endpoints with decorators
for JSON body, query parameters, and form data validation.

Usage:
    from flask_core.validation import validate_json, validate_query, PaginationParams

    @api_bp.route('/users', methods=['GET'])
    @validate_query(PaginationParams)
    async def list_users(query_params: PaginationParams):
        users = await get_users(limit=query_params.limit, offset=query_params.offset)
        return success_response(users)
"""

from pydantic import BaseModel, Field, validator, ValidationError
from quart import request
from typing import Optional, Callable, Any, Dict, List
from functools import wraps
from datetime import datetime
import re
import logging

# Import from shared library for re-export (penguin-libs when available, graceful fallback)
try:
    from py_libs.models import (
        WaddleBaseModel, WaddleRequestModel, Platform,
        PaginationParams as SharedPaginationParams,
        CommunityIdRequired as SharedCommunityIdRequired,
        DateRange as SharedDateRange,
    )
    HAS_SHARED_MODELS = True
except ImportError:
    HAS_SHARED_MODELS = False

logger = logging.getLogger(__name__)


# ============================================================================
# Common Validation Models
# ============================================================================

class PaginationParams(BaseModel):
    """Standard pagination parameters with sensible defaults."""
    limit: int = Field(default=50, ge=1, le=100, description="Number of items per page")
    offset: int = Field(default=0, ge=0, description="Number of items to skip")

    class Config:
        extra = 'forbid'  # Reject unknown fields


class CommunityIdRequired(BaseModel):
    """Base model requiring a valid community_id."""
    community_id: int = Field(..., gt=0, description="Community ID (must be positive)")

    class Config:
        extra = 'forbid'


class UsernameRequired(BaseModel):
    """Base model requiring a valid username."""
    username: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Username"
    )

    @validator('username')
    def validate_username(cls, v):
        """Validate username format."""
        if not v or not v.strip():
            raise ValueError('username cannot be empty or whitespace only')
        return v.strip()

    class Config:
        extra = 'forbid'


class DateRange(BaseModel):
    """Date range validation with optional start/end dates."""
    start_date: Optional[datetime] = Field(None, description="Start date (ISO 8601)")
    end_date: Optional[datetime] = Field(None, description="End date (ISO 8601)")

    @validator('end_date')
    def validate_date_range(cls, v, values):
        """Ensure end_date is after start_date."""
        if v and 'start_date' in values and values['start_date']:
            if v <= values['start_date']:
                raise ValueError('end_date must be after start_date')
        return v

    class Config:
        extra = 'forbid'


class PlatformRequired(BaseModel):
    """Base model requiring a valid platform."""
    platform: str = Field(
        ...,
        pattern=r'^(twitch|discord|slack|kick)$',
        description="Platform name"
    )

    class Config:
        extra = 'forbid'


# ============================================================================
# Custom Validators
# ============================================================================

def validate_email(v: str) -> str:
    """Validate email format."""
    if not v:
        raise ValueError('email cannot be empty')

    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(email_pattern, v):
        raise ValueError('invalid email format')

    return v.lower().strip()


def validate_url(v: str) -> str:
    """Validate URL format."""
    if not v:
        raise ValueError('URL cannot be empty')

    url_pattern = r'^https?://[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(/.*)?$'
    if not re.match(url_pattern, v):
        raise ValueError('invalid URL format (must start with http:// or https://)')

    return v.strip()


def validate_username_format(v: str) -> str:
    """Validate username format (alphanumeric, hyphens, underscores)."""
    if not v:
        raise ValueError('username cannot be empty')

    if len(v) < 3 or len(v) > 50:
        raise ValueError('username must be 3-50 characters')

    username_pattern = r'^[a-zA-Z0-9_-]+$'
    if not re.match(username_pattern, v):
        raise ValueError('username can only contain letters, numbers, hyphens, and underscores')

    return v.strip()


def validate_positive_integer(v: int, field_name: str = "value") -> int:
    """Validate positive integer."""
    if v <= 0:
        raise ValueError(f'{field_name} must be positive')
    return v


def validate_non_negative_integer(v: int, field_name: str = "value") -> int:
    """Validate non-negative integer."""
    if v < 0:
        raise ValueError(f'{field_name} must be non-negative')
    return v


# ============================================================================
# Validation Decorators
# ============================================================================

def validate_json(model: type[BaseModel], strict: bool = True):
    """
    Decorator to validate JSON request body against a Pydantic model.

    Args:
        model: Pydantic BaseModel class to validate against
        strict: If True, return 400 on validation error. If False, log and continue.

    Usage:
        @api_bp.route('/users', methods=['POST'])
        @validate_json(UserCreateRequest)
        async def create_user(validated_data: UserCreateRequest):
            user = await create_user_service(validated_data.dict())
            return success_response(user)
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        async def decorated_function(*args, **kwargs):
            try:
                # Get JSON data from request
                data = await request.get_json()

                if data is None:
                    logger.warning(f"AUTHZ validation_failed endpoint={request.endpoint} error=empty_json_body")
                    return {
                        'status': 'error',
                        'message': 'Request body must be valid JSON',
                        'errors': []
                    }, 400

                # Validate against Pydantic model
                validated_data = model.parse_obj(data)

                # Log successful validation
                logger.debug(f"AUTHZ validation_success endpoint={request.endpoint} model={model.__name__}")

                # Pass validated data to the endpoint
                return await f(validated_data, *args, **kwargs)

            except ValidationError as e:
                # Convert Pydantic errors to API response format
                errors = []
                for error in e.errors():
                    field = '.'.join(str(loc) for loc in error['loc'])
                    errors.append({
                        'field': field,
                        'message': error['msg'],
                        'type': error['type']
                    })

                logger.warning(
                    f"AUTHZ validation_failed endpoint={request.endpoint} "
                    f"model={model.__name__} errors={len(errors)}"
                )

                if strict:
                    return {
                        'status': 'error',
                        'message': 'Validation failed',
                        'errors': errors
                    }, 400
                else:
                    # Log but continue with original data
                    logger.error(f"Validation errors: {errors}")
                    return await f(data, *args, **kwargs)

            except Exception as e:
                logger.error(
                    f"ERROR validation_exception endpoint={request.endpoint} "
                    f"error={str(e)}"
                )
                return {
                    'status': 'error',
                    'message': 'Invalid request data',
                    'errors': []
                }, 400

        return decorated_function
    return decorator


def validate_query(model: type[BaseModel], strict: bool = True):
    """
    Decorator to validate query parameters against a Pydantic model.

    Args:
        model: Pydantic BaseModel class to validate against
        strict: If True, return 400 on validation error. If False, log and continue.

    Usage:
        @api_bp.route('/users', methods=['GET'])
        @validate_query(PaginationParams)
        async def list_users(query_params: PaginationParams):
            users = await get_users(limit=query_params.limit)
            return success_response(users)
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        async def decorated_function(*args, **kwargs):
            try:
                # Get query parameters from request
                query_data = request.args.to_dict()

                # Convert to appropriate types for Pydantic
                # (query params are always strings, need conversion)
                converted_data = {}
                for key, value in query_data.items():
                    # Try to convert to int if it looks like a number
                    if value.isdigit() or (value.startswith('-') and value[1:].isdigit()):
                        converted_data[key] = int(value)
                    # Try to convert to float
                    elif '.' in value:
                        try:
                            converted_data[key] = float(value)
                        except ValueError:
                            converted_data[key] = value
                    # Boolean conversion
                    elif value.lower() in ('true', 'false'):
                        converted_data[key] = value.lower() == 'true'
                    else:
                        converted_data[key] = value

                # Validate against Pydantic model
                validated_data = model.parse_obj(converted_data)

                # Log successful validation
                logger.debug(f"AUTHZ validation_success endpoint={request.endpoint} model={model.__name__}")

                # Pass validated data to the endpoint
                return await f(validated_data, *args, **kwargs)

            except ValidationError as e:
                # Convert Pydantic errors to API response format
                errors = []
                for error in e.errors():
                    field = '.'.join(str(loc) for loc in error['loc'])
                    errors.append({
                        'field': field,
                        'message': error['msg'],
                        'type': error['type']
                    })

                logger.warning(
                    f"AUTHZ validation_failed endpoint={request.endpoint} "
                    f"model={model.__name__} errors={len(errors)}"
                )

                if strict:
                    return {
                        'status': 'error',
                        'message': 'Invalid query parameters',
                        'errors': errors
                    }, 400
                else:
                    # Log but continue with original data
                    logger.error(f"Validation errors: {errors}")
                    return await f(query_data, *args, **kwargs)

            except Exception as e:
                logger.error(
                    f"ERROR validation_exception endpoint={request.endpoint} "
                    f"error={str(e)}"
                )
                return {
                    'status': 'error',
                    'message': 'Invalid query parameters',
                    'errors': []
                }, 400

        return decorated_function
    return decorator


def validate_form(model: type[BaseModel], strict: bool = True):
    """
    Decorator to validate form data against a Pydantic model.

    Args:
        model: Pydantic BaseModel class to validate against
        strict: If True, return 400 on validation error. If False, log and continue.

    Usage:
        @api_bp.route('/upload', methods=['POST'])
        @validate_form(FileUploadRequest)
        async def upload_file(validated_data: FileUploadRequest):
            result = await upload_service(validated_data.dict())
            return success_response(result)
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        async def decorated_function(*args, **kwargs):
            try:
                # Get form data from request
                form_data = await request.form
                form_dict = form_data.to_dict()

                # Validate against Pydantic model
                validated_data = model.parse_obj(form_dict)

                # Log successful validation
                logger.debug(f"AUTHZ validation_success endpoint={request.endpoint} model={model.__name__}")

                # Pass validated data to the endpoint
                return await f(validated_data, *args, **kwargs)

            except ValidationError as e:
                # Convert Pydantic errors to API response format
                errors = []
                for error in e.errors():
                    field = '.'.join(str(loc) for loc in error['loc'])
                    errors.append({
                        'field': field,
                        'message': error['msg'],
                        'type': error['type']
                    })

                logger.warning(
                    f"AUTHZ validation_failed endpoint={request.endpoint} "
                    f"model={model.__name__} errors={len(errors)}"
                )

                if strict:
                    return {
                        'status': 'error',
                        'message': 'Invalid form data',
                        'errors': errors
                    }, 400
                else:
                    # Log but continue with original data
                    logger.error(f"Validation errors: {errors}")
                    return await f(form_dict, *args, **kwargs)

            except Exception as e:
                logger.error(
                    f"ERROR validation_exception endpoint={request.endpoint} "
                    f"error={str(e)}"
                )
                return {
                    'status': 'error',
                    'message': 'Invalid form data',
                    'errors': []
                }, 400

        return decorated_function
    return decorator


# ============================================================================
# Helper Functions
# ============================================================================

def validate_data(model: type[BaseModel], data: Dict[str, Any]) -> tuple[bool, Any, List[Dict]]:
    """
    Validate data against a Pydantic model programmatically.

    Args:
        model: Pydantic BaseModel class to validate against
        data: Dictionary of data to validate

    Returns:
        Tuple of (is_valid, validated_data_or_errors, error_list)

    Usage:
        is_valid, result, errors = validate_data(UserCreateRequest, user_data)
        if is_valid:
            # result is validated_data
            await create_user(result.dict())
        else:
            # result is None, errors contains validation errors
            return error_response("Validation failed", errors=errors)
    """
    try:
        validated = model.parse_obj(data)
        return True, validated, []
    except ValidationError as e:
        errors = []
        for error in e.errors():
            field = '.'.join(str(loc) for loc in error['loc'])
            errors.append({
                'field': field,
                'message': error['msg'],
                'type': error['type']
            })
        return False, None, errors


__all__ = [
    # Common models
    'PaginationParams',
    'CommunityIdRequired',
    'UsernameRequired',
    'DateRange',
    'PlatformRequired',
    # Validators
    'validate_email',
    'validate_url',
    'validate_username_format',
    'validate_positive_integer',
    'validate_non_negative_integer',
    # Decorators
    'validate_json',
    'validate_query',
    'validate_form',
    # Helpers
    'validate_data',
    # Pydantic imports for convenience
    'BaseModel',
    'Field',
    'validator',
    'ValidationError',
    # Shared library models
    'WaddleBaseModel',
    'WaddleRequestModel',
    'Platform',
    'HAS_SHARED_MODELS',
]
