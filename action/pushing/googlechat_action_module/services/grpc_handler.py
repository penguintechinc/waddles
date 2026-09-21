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
    from proto import googlechat_action_pb2, googlechat_action_pb2_grpc
except ImportError:
    # Proto files not generated yet
    googlechat_action_pb2 = None
    googlechat_action_pb2_grpc = None

from services.googlechat_service import GoogleChatService
from services.grpc_auth_interceptor import AuthInterceptor  # noqa: E402
from services.grpc_tls import bind_secure_port, default_server_options  # noqa: E402


logger = logging.getLogger(__name__)


class GoogleChatActionServicer:
    """gRPC service implementation for Google Chat actions"""

    def __init__(self, googlechat_service: GoogleChatService):
        """
        Initialize gRPC servicer

        Args:
            googlechat_service: Google Chat service instance
        """
        self.googlechat_service = googlechat_service

    async def SendMessage(self, request, context):
        """Handle SendMessage gRPC call"""
        try:
            cards = None
            if request.cards_json:
                cards = json.loads(request.cards_json)

            result = await self.googlechat_service.send_message(
                community_id=request.community_id,
                space_id=request.space_id,
                text=request.text if request.text else None,
                cards=cards,
                thread_id=request.thread_id if request.thread_id else None
            )

            if googlechat_action_pb2:
                return googlechat_action_pb2.SendMessageResponse(
                    success=result['success'],
                    message_id=result['message_id'] or '',
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"SendMessage error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if googlechat_action_pb2:
                return googlechat_action_pb2.SendMessageResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def UpdateMessage(self, request, context):
        """Handle UpdateMessage gRPC call"""
        try:
            cards = None
            if request.cards_json:
                cards = json.loads(request.cards_json)

            result = await self.googlechat_service.update_message(
                community_id=request.community_id,
                message_id=request.message_id,
                text=request.text if request.text else None,
                cards=cards
            )

            if googlechat_action_pb2:
                return googlechat_action_pb2.UpdateMessageResponse(
                    success=result['success'],
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"UpdateMessage error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if googlechat_action_pb2:
                return googlechat_action_pb2.UpdateMessageResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def DeleteMessage(self, request, context):
        """Handle DeleteMessage gRPC call"""
        try:
            result = await self.googlechat_service.delete_message(
                community_id=request.community_id,
                message_id=request.message_id
            )

            if googlechat_action_pb2:
                return googlechat_action_pb2.DeleteMessageResponse(
                    success=result['success'],
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"DeleteMessage error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if googlechat_action_pb2:
                return googlechat_action_pb2.DeleteMessageResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def CreateSpace(self, request, context):
        """Handle CreateSpace gRPC call"""
        try:
            result = await self.googlechat_service.create_space(
                community_id=request.community_id,
                display_name=request.display_name,
                space_type=request.space_type if request.space_type else "SPACE",
                description=request.description if request.description else None
            )

            if googlechat_action_pb2:
                return googlechat_action_pb2.CreateSpaceResponse(
                    success=result['success'],
                    space_id=result['space_id'] or '',
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"CreateSpace error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if googlechat_action_pb2:
                return googlechat_action_pb2.CreateSpaceResponse(
                    success=False,
                    error=str(e)
                )
            return None


def create_grpc_server(googlechat_service: GoogleChatService, port: int, max_workers: int = 10):
    """
    Create and configure gRPC server

    Args:
        googlechat_service: Google Chat service instance
        port: gRPC server port
        max_workers: Maximum number of worker threads

    Returns:
        Configured gRPC server instance
    """
    if not googlechat_action_pb2_grpc:
        logger.error("Proto files not generated. Run: python -m grpc_tools.protoc")
        return None

    server = grpc.aio.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        interceptors=[AuthInterceptor()],
        options=default_server_options(),
    )

    servicer = GoogleChatActionServicer(googlechat_service)
    googlechat_action_pb2_grpc.add_GoogleChatActionServiceServicer_to_server(servicer, server)

    bind_secure_port(server, f'[::]:{port}')

    logger.info(f"gRPC server (TLS) configured on port {port}")
    return server
