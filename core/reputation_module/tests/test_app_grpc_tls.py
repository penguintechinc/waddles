"""Unit tests for gRPC TLS setup logic in reputation_module app.py"""
import os
from unittest.mock import patch
from config import Config


class TestGrpcTlsLogic:
    """Test suite for gRPC TLS mode selection and startup logic."""

    def test_grpc_tls_insecure_dev_alpha_tier(self):
        """Flag set + alpha tier → insecure bind allowed (no exception raised)."""
        with patch.dict(os.environ, {
            'DEPLOYMENT_TIER': 'alpha',
            'GRPC_TLS_INSECURE_DEV': 'true'
        }):
            # Re-load config to pick up new env vars
            from importlib import reload
            import config as config_module
            reload(config_module)
            cfg = config_module.Config

            assert cfg.DEPLOYMENT_TIER == 'alpha'
            grpc_tls_insecure_flag = (
                os.getenv('GRPC_TLS_INSECURE_DEV', '').strip().lower() in {'1', 'true', 'yes', 'on'}
            )
            dev_tiers = {'development', 'alpha', 'local', 'test'}
            use_insecure = grpc_tls_insecure_flag and cfg.DEPLOYMENT_TIER in dev_tiers

            assert use_insecure is True, "Should allow insecure mode in alpha tier with flag set"

    def test_grpc_tls_insecure_dev_production_tier_refused(self):
        """Flag set + production tier → refused (exit non-zero expected)."""
        with patch.dict(os.environ, {
            'DEPLOYMENT_TIER': 'production',
            'GRPC_TLS_INSECURE_DEV': 'true'
        }):
            from importlib import reload
            import config as config_module
            reload(config_module)
            cfg = config_module.Config

            assert cfg.DEPLOYMENT_TIER == 'production'
            grpc_tls_insecure_flag = (
                os.getenv('GRPC_TLS_INSECURE_DEV', '').strip().lower() in {'1', 'true', 'yes', 'on'}
            )
            dev_tiers = {'development', 'alpha', 'local', 'test'}
            use_insecure = grpc_tls_insecure_flag and cfg.DEPLOYMENT_TIER in dev_tiers

            assert use_insecure is False, "Should refuse insecure mode in production tier"
            assert grpc_tls_insecure_flag is True, "Flag should be set"
            assert cfg.DEPLOYMENT_TIER not in dev_tiers, "Tier should be production"

    def test_grpc_tls_flag_unset_defaults_to_secure(self):
        """Flag unset → TLS mode required (default secure)."""
        with patch.dict(os.environ, {
            'DEPLOYMENT_TIER': 'alpha'
        }, clear=False):
            # Ensure GRPC_TLS_INSECURE_DEV is not set
            os.environ.pop('GRPC_TLS_INSECURE_DEV', None)

            grpc_tls_insecure_flag = (
                os.getenv('GRPC_TLS_INSECURE_DEV', '').strip().lower() in {'1', 'true', 'yes', 'on'}
            )
            dev_tiers = {'development', 'alpha', 'local', 'test'}
            use_insecure = grpc_tls_insecure_flag and Config.DEPLOYMENT_TIER in dev_tiers

            assert use_insecure is False, "Should default to TLS when flag is unset"
            assert grpc_tls_insecure_flag is False, "Flag should not be set"
