"""
gRPC Handler - Handles incoming gRPC requests from processor/router
"""
import json
import logging
from typing import Optional
import grpc
from concurrent import futures

# Import generated proto files
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

try:
    from proto import teams_action_pb2, teams_action_pb2_grpc
except ImportError:
    # Proto files not generated yet
    teams_action_pb2 = None
    teams_action_pb2_grpc = None

from services.teams_service import TeamsService
from services.grpc_auth_interceptor import AuthInterceptor  # noqa: E402
from services.grpc_tls import bind_secure_port, default_server_options  # noqa: E402


logger = logging.getLogger(__name__)


class TeamsActionServicer:
    """gRPC service implementation for Teams actions"""

    def __init__(self, teams_service: TeamsService):
        """
        Initialize gRPC servicer

        Args:
            teams_service: Teams service instance
        """
        self.teams_service = teams_service

    async def SendMessage(self, request, context):
        """Handle SendMessage gRPC call"""
        try:
            blocks = None
            if request.blocks_json:
                blocks = json.loads(request.blocks_json)

            result = await self.teams_service.send_message(
                community_id=request.community_id,
                channel_id=request.channel_id,
                text=request.text,
                blocks=blocks,
                thread_ts=request.thread_ts if request.thread_ts else None
            )

            if teams_action_pb2:
                return teams_action_pb2.SendMessageResponse(
                    success=result['success'],
                    message_id=result['message_id'] or '',
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"SendMessage error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if teams_action_pb2:
                return teams_action_pb2.SendMessageResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def SendEphemeral(self, request, context):
        """Handle SendEphemeral gRPC call"""
        try:
            result = await self.teams_service.send_ephemeral(
                community_id=request.community_id,
                channel_id=request.channel_id,
                user_id=request.user_id,
                text=request.text
            )

            if teams_action_pb2:
                return teams_action_pb2.SendEphemeralResponse(
                    success=result['success'],
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"SendEphemeral error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if teams_action_pb2:
                return teams_action_pb2.SendEphemeralResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def UpdateMessage(self, request, context):
        """Handle UpdateMessage gRPC call"""
        try:
            blocks = None
            if request.blocks_json:
                blocks = json.loads(request.blocks_json)

            result = await self.teams_service.update_message(
                community_id=request.community_id,
                channel_id=request.channel_id,
                ts=request.ts,
                text=request.text,
                blocks=blocks
            )

            if teams_action_pb2:
                return teams_action_pb2.UpdateMessageResponse(
                    success=result['success'],
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"UpdateMessage error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if teams_action_pb2:
                return teams_action_pb2.UpdateMessageResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def DeleteMessage(self, request, context):
        """Handle DeleteMessage gRPC call"""
        try:
            result = await self.teams_service.delete_message(
                community_id=request.community_id,
                channel_id=request.channel_id,
                ts=request.ts
            )

            if teams_action_pb2:
                return teams_action_pb2.DeleteMessageResponse(
                    success=result['success'],
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"DeleteMessage error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if teams_action_pb2:
                return teams_action_pb2.DeleteMessageResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def OpenModal(self, request, context):
        """Handle OpenModal gRPC call"""
        try:
            view = None
            if request.view_json:
                view = json.loads(request.view_json)

            result = await self.teams_service.open_modal(
                community_id=request.community_id,
                trigger_id=request.trigger_id,
                view=view
            )

            if teams_action_pb2:
                return teams_action_pb2.OpenModalResponse(
                    success=result['success'],
                    view_id=result['view_id'] or '',
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"OpenModal error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if teams_action_pb2:
                return teams_action_pb2.OpenModalResponse(
                    success=False,
                    error=str(e)
                )
            return None


def create_grpc_server(teams_service: TeamsService, port: int, max_workers: int = 10):
    """
    Create and configure gRPC server

    Args:
        teams_service: Teams service instance
        port: gRPC server port
        max_workers: Maximum number of worker threads

    Returns:
        Configured gRPC server instance
    """
    if not teams_action_pb2_grpc:
        logger.error("Proto files not generated. Run: python -m grpc_tools.protoc")
        return None

    server = grpc.aio.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        interceptors=[AuthInterceptor()],
        options=default_server_options(),
    )

    servicer = TeamsActionServicer(teams_service)
    teams_action_pb2_grpc.add_TeamsActionServiceServicer_to_server(servicer, server)

    bind_secure_port(server, f'[::]:{port}')

    logger.info(f"gRPC server (TLS) configured on port {port}")
    return server
