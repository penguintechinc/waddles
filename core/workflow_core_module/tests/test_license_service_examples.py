"""
License Service Usage Examples and Integration Tests

This file demonstrates:
1. Initialization and configuration
2. License checking and validation
3. Error handling
4. Caching behavior
5. Development vs production modes
6. Integration with workflow controllers
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta

from services.license_service import (
    LicenseService,
    LicenseStatus,
    LicenseTier,
    LicenseException,
    LicenseValidationException
)


# ============================================================================
# Example 1: Basic Initialization
# ============================================================================

async def example_basic_initialization():
    """Initialize license service in development mode"""
    license_service = LicenseService(
        license_server_url="https://license.penguintech.io",
        redis_url="redis://localhost:6379/0",
        release_mode=False,  # Dev mode - skips license enforcement
    )

    # Connect to Redis and aiohttp
    await license_service.connect()

    try:
        # Check license (will assume premium in dev mode)
        status = await license_service.check_license_status(
            community_id=123
        )
        print(f"License status: {status}")
        # Output: License status: {'status': 'active', 'tier': 'premium', ...}

    finally:
        await license_service.disconnect()


# ============================================================================
# Example 2: Production License Validation
# ============================================================================

async def example_production_validation():
    """Validate licenses against PenguinTech server in production"""
    license_service = LicenseService(
        license_server_url="https://license.penguintech.io",
        redis_url="redis://localhost:6379/0",
        release_mode=True,  # Production mode - enforce licenses
    )

    await license_service.connect()

    try:
        # This would hit the PenguinTech License Server
        status = await license_service.check_license_status(
            community_id=456,
            license_key="PENG-XXXX-XXXX-XXXX-XXXX-ABCD"
        )

        print(f"Production license check: {status}")
        # Output: {'status': 'active', 'tier': 'premium', 'expires_at': '2025-12-31', ...}

    except LicenseException as e:
        print(f"License check failed: {e}")

    finally:
        await license_service.disconnect()


# ============================================================================
# Example 3: Workflow Creation Validation
# ============================================================================

async def example_workflow_creation():
    """Validate before creating a workflow"""
    license_service = LicenseService(
        license_server_url="https://license.penguintech.io",
        redis_url="redis://localhost:6379/0",
        release_mode=False,  # Dev mode for example
    )

    await license_service.connect()

    try:
        # Premium tier - should succeed
        result = await license_service.validate_workflow_creation(
            community_id=789,
            entity_id="workflow_uuid_123",
            license_key=None  # Optional, would be fetched from DB
        )
        print(f"Workflow creation validation passed: {result}")

    except LicenseValidationException as e:
        print(f"Workflow creation denied: {e.message}")
        print(f"HTTP Status Code: {e.status_code}")  # 402
        print(f"Community ID: {e.community_id}")

    finally:
        await license_service.disconnect()


# ============================================================================
# Example 4: Workflow Execution Validation
# ============================================================================

async def example_workflow_execution():
    """Validate before executing a workflow"""
    license_service = LicenseService(
        license_server_url="https://license.penguintech.io",
        redis_url="redis://localhost:6379/0",
        release_mode=False,
    )

    await license_service.connect()

    try:
        # Validate execution
        result = await license_service.validate_workflow_execution(
            workflow_id="workflow_uuid_456",
            community_id=999,
            license_key=None
        )
        print(f"Workflow execution validated: {result}")

    except LicenseValidationException as e:
        print(f"Workflow execution denied: {e.message}")
        # Return HTTP 402 Payment Required to client

    finally:
        await license_service.disconnect()


# ============================================================================
# Example 5: Get License Information
# ============================================================================

async def example_get_license_info():
    """Retrieve complete license information"""
    license_service = LicenseService(
        license_server_url="https://license.penguintech.io",
        redis_url="redis://localhost:6379/0",
        release_mode=False,
    )

    await license_service.connect()

    try:
        info = await license_service.get_license_info(
            community_id=111
        )

        print(f"License Information:")
        print(f"  Tier: {info['tier']}")
        print(f"  Status: {info['status']}")
        print(f"  Expires: {info['expires_at']}")
        print(f"  Workflow Limit: {info['workflow_limit']}")
        print(f"  Cached: {info['cached']}")
        print(f"  Features: {info['features']}")

    finally:
        await license_service.disconnect()


# ============================================================================
# Example 6: Cache Invalidation
# ============================================================================

async def example_cache_invalidation():
    """Manually invalidate cached license"""
    license_service = LicenseService(
        license_server_url="https://license.penguintech.io",
        redis_url="redis://localhost:6379/0",
        release_mode=False,
    )

    await license_service.connect()

    try:
        # First check (hits server or caches)
        info1 = await license_service.check_license_status(222)

        # Invalidate cache (e.g., after admin updates license)
        await license_service.invalidate_cache(222)

        # Next check will fetch fresh data from server
        info2 = await license_service.check_license_status(222)

    finally:
        await license_service.disconnect()


# ============================================================================
# Unit Tests
# ============================================================================

class TestLicenseService:
    """Unit tests for license service"""

    @pytest.mark.asyncio
    async def test_check_license_status_dev_mode(self):
        """Test license check in development mode"""
        service = LicenseService(
            license_server_url="http://localhost:8080",
            release_mode=False
        )

        status = await service.check_license_status(123)

        assert status["status"] == LicenseStatus.ACTIVE.value
        assert status["tier"] == LicenseTier.PREMIUM.value
        assert status["dev_mode"] is True

    @pytest.mark.asyncio
    async def test_validate_workflow_creation_dev_mode(self):
        """Test workflow creation validation in dev mode"""
        service = LicenseService(
            license_server_url="http://localhost:8080",
            release_mode=False
        )

        result = await service.validate_workflow_creation(
            community_id=123,
            entity_id="wf_1"
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_validate_workflow_creation_premium_allowed(self):
        """Test that premium tier can create workflows"""
        service = LicenseService(
            license_server_url="http://localhost:8080",
            release_mode=True
        )

        # Mock the server response
        with patch.object(
            service,
            '_validate_with_server',
            return_value={
                "status": LicenseStatus.ACTIVE.value,
                "tier": LicenseTier.PREMIUM.value,
                "features": {"workflows": True},
                "cached": False
            }
        ):
            result = await service.validate_workflow_creation(
                community_id=123,
                entity_id="wf_1",
                license_key="PENG-XXXX-XXXX-XXXX-XXXX-ABCD"
            )

            assert result is True

    @pytest.mark.asyncio
    async def test_validate_workflow_creation_free_denied(self):
        """Test that free tier cannot create workflows"""
        service = LicenseService(
            license_server_url="http://localhost:8080",
            release_mode=True
        )

        # Mock the server response
        with patch.object(
            service,
            '_validate_with_server',
            return_value={
                "status": LicenseStatus.ACTIVE.value,
                "tier": LicenseTier.FREE.value,
                "features": {"workflows": False},
                "cached": False
            }
        ):
            with pytest.raises(LicenseValidationException) as exc:
                await service.validate_workflow_creation(
                    community_id=123,
                    entity_id="wf_1",
                    license_key="PENG-XXXX-XXXX-XXXX-XXXX-ABCD"
                )

            assert exc.value.status_code == 402
            assert exc.value.community_id == 123

    @pytest.mark.asyncio
    async def test_validate_workflow_execution_expired_license(self):
        """Test that expired license cannot execute workflows"""
        service = LicenseService(
            license_server_url="http://localhost:8080",
            release_mode=True
        )

        with patch.object(
            service,
            '_validate_with_server',
            return_value={
                "status": LicenseStatus.EXPIRED.value,
                "tier": LicenseTier.PREMIUM.value,
                "features": {"workflows": False},
                "cached": False
            }
        ):
            with pytest.raises(LicenseValidationException):
                await service.validate_workflow_execution(
                    workflow_id="wf_1",
                    community_id=123,
                    license_key="PENG-XXXX-XXXX-XXXX-XXXX-ABCD"
                )

    @pytest.mark.asyncio
    async def test_get_license_info_premium(self):
        """Test getting license info for premium tier"""
        service = LicenseService(
            license_server_url="http://localhost:8080",
            release_mode=False
        )

        info = await service.get_license_info(456)

        assert info["tier"] == LicenseTier.PREMIUM.value
        assert info["status"] == LicenseStatus.ACTIVE.value
        assert info["workflow_limit"] is None  # Unlimited for premium

    @pytest.mark.asyncio
    async def test_get_license_info_free(self):
        """Test getting license info for free tier"""
        service = LicenseService(
            license_server_url="http://localhost:8080",
            release_mode=True
        )

        with patch.object(
            service,
            '_validate_with_server',
            return_value={
                "status": LicenseStatus.ACTIVE.value,
                "tier": LicenseTier.FREE.value,
                "features": {"workflows": False},
                "cached": False
            }
        ):
            info = await service.get_license_info(
                456,
                license_key="PENG-XXXX-XXXX-XXXX-XXXX-ABCD"
            )

            assert info["tier"] == LicenseTier.FREE.value
            assert info["workflow_limit"] == 0  # 0 workflows for free

    @pytest.mark.asyncio
    async def test_cache_license_status(self):
        """Test that license status is cached"""
        service = LicenseService(
            license_server_url="http://localhost:8080",
            release_mode=True
        )

        # Mock validation
        mock_validate = AsyncMock(
            return_value={
                "status": LicenseStatus.ACTIVE.value,
                "tier": LicenseTier.PREMIUM.value,
                "features": {"workflows": True},
                "cached": False
            }
        )

        with patch.object(service, '_validate_with_server', mock_validate):
            # First call
            result1 = await service.check_license_status(
                789,
                license_key="PENG-XXXX-XXXX-XXXX-XXXX-ABCD"
            )

            # Second call should use cache
            result2 = await service.check_license_status(789)

            # Should only call validate_with_server once
            assert mock_validate.call_count == 1
            assert result1["status"] == result2["status"]


# ============================================================================
# Integration Examples with Quart/Flask Controllers
# ============================================================================

async def example_quart_controller():
    """Example of using license service in a Quart controller"""
    from quart import Blueprint, request, jsonify

    # Mock setup
    license_service = LicenseService(
        license_server_url="https://license.penguintech.io",
        redis_url="redis://localhost:6379/0",
        release_mode=True
    )
    await license_service.connect()

    api_bp = Blueprint('api', __name__, url_prefix='/api/v1')

    @api_bp.route('/workflows', methods=['POST'])
    async def create_workflow():
        """Create workflow endpoint with license validation"""
        try:
            data = await request.get_json()
            community_id = data.get('community_id')
            workflow_id = data.get('workflow_id')

            # Validate license before creating
            await license_service.validate_workflow_creation(
                community_id=community_id,
                entity_id=workflow_id
            )

            # Create workflow...
            return jsonify({
                "status": "success",
                "workflow_id": workflow_id
            }), 201

        except LicenseValidationException as e:
            return jsonify({
                "error": True,
                "message": e.message,
                "community_id": e.community_id
            }), 402

        except Exception as e:
            return jsonify({
                "error": True,
                "message": str(e)
            }), 500

    @api_bp.route('/workflows/<workflow_id>/execute', methods=['POST'])
    async def execute_workflow(workflow_id: str):
        """Execute workflow endpoint with license validation"""
        try:
            data = await request.get_json()
            community_id = data.get('community_id')

            # Validate execution
            await license_service.validate_workflow_execution(
                workflow_id=workflow_id,
                community_id=community_id
            )

            # Execute workflow...
            return jsonify({
                "status": "executing",
                "workflow_id": workflow_id
            }), 202

        except LicenseValidationException as e:
            return jsonify({
                "error": True,
                "message": e.message,
            }), 402

        except Exception as e:
            return jsonify({
                "error": True,
                "message": str(e)
            }), 500

    @api_bp.route('/license/info', methods=['GET'])
    async def get_license_info():
        """Get license information endpoint"""
        try:
            community_id = request.args.get('community_id', type=int)
            info = await license_service.get_license_info(community_id)

            return jsonify({
                "status": "success",
                "license": info
            }), 200

        except Exception as e:
            return jsonify({
                "error": True,
                "message": str(e)
            }), 500


# ============================================================================
# Run Examples
# ============================================================================

if __name__ == "__main__":
    print("License Service Examples")
    print("=" * 50)
    print("\nNote: These examples require proper configuration.")
    print("Run with: pytest test_license_service_examples.py -v")
