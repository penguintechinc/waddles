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
    from proto import slack_action_pb2, slack_action_pb2_grpc
except ImportError:
    # Proto files not generated yet
    slack_action_pb2 = None
    slack_action_pb2_grpc = None

from services.slack_service import SlackService
from services.grpc_auth_interceptor import AuthInterceptor  # noqa: E402
from services.grpc_tls import bind_secure_port, default_server_options  # noqa: E402


logger = logging.getLogger(__name__)


class SlackActionServicer:
    """gRPC service implementation for Slack actions"""

    def __init__(self, slack_service: SlackService):
        """
        Initialize gRPC servicer

        Args:
            slack_service: Slack service instance
        """
        self.slack_service = slack_service

    async def SendMessage(self, request, context):
        """Handle SendMessage gRPC call"""
        try:
            blocks = None
            if request.blocks_json:
                blocks = json.loads(request.blocks_json)

            result = await self.slack_service.send_message(
                community_id=request.community_id,
                channel_id=request.channel_id,
                text=request.text,
                blocks=blocks,
                thread_ts=request.thread_ts if request.thread_ts else None
            )

            if slack_action_pb2:
                return slack_action_pb2.SendMessageResponse(
                    success=result['success'],
                    message_ts=result['message_ts'] or '',
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"SendMessage error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if slack_action_pb2:
                return slack_action_pb2.SendMessageResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def SendEphemeral(self, request, context):
        """Handle SendEphemeral gRPC call"""
        try:
            result = await self.slack_service.send_ephemeral(
                community_id=request.community_id,
                channel_id=request.channel_id,
                user_id=request.user_id,
                text=request.text
            )

            if slack_action_pb2:
                return slack_action_pb2.SendEphemeralResponse(
                    success=result['success'],
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"SendEphemeral error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if slack_action_pb2:
                return slack_action_pb2.SendEphemeralResponse(
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

            result = await self.slack_service.update_message(
                community_id=request.community_id,
                channel_id=request.channel_id,
                ts=request.ts,
                text=request.text,
                blocks=blocks
            )

            if slack_action_pb2:
                return slack_action_pb2.UpdateMessageResponse(
                    success=result['success'],
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"UpdateMessage error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if slack_action_pb2:
                return slack_action_pb2.UpdateMessageResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def DeleteMessage(self, request, context):
        """Handle DeleteMessage gRPC call"""
        try:
            result = await self.slack_service.delete_message(
                community_id=request.community_id,
                channel_id=request.channel_id,
                ts=request.ts
            )

            if slack_action_pb2:
                return slack_action_pb2.DeleteMessageResponse(
                    success=result['success'],
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"DeleteMessage error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if slack_action_pb2:
                return slack_action_pb2.DeleteMessageResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def AddReaction(self, request, context):
        """Handle AddReaction gRPC call"""
        try:
            result = await self.slack_service.add_reaction(
                community_id=request.community_id,
                channel_id=request.channel_id,
                ts=request.ts,
                emoji=request.emoji
            )

            if slack_action_pb2:
                return slack_action_pb2.AddReactionResponse(
                    success=result['success'],
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"AddReaction error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if slack_action_pb2:
                return slack_action_pb2.AddReactionResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def RemoveReaction(self, request, context):
        """Handle RemoveReaction gRPC call"""
        try:
            result = await self.slack_service.remove_reaction(
                community_id=request.community_id,
                channel_id=request.channel_id,
                ts=request.ts,
                emoji=request.emoji
            )

            if slack_action_pb2:
                return slack_action_pb2.RemoveReactionResponse(
                    success=result['success'],
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"RemoveReaction error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if slack_action_pb2:
                return slack_action_pb2.RemoveReactionResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def UploadFile(self, request, context):
        """Handle UploadFile gRPC call"""
        try:
            result = await self.slack_service.upload_file(
                community_id=request.community_id,
                channel_id=request.channel_id,
                file_content=request.file_content,
                filename=request.filename,
                title=request.title
            )

            if slack_action_pb2:
                return slack_action_pb2.UploadFileResponse(
                    success=result['success'],
                    file_id=result['file_id'] or '',
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"UploadFile error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if slack_action_pb2:
                return slack_action_pb2.UploadFileResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def CreateChannel(self, request, context):
        """Handle CreateChannel gRPC call"""
        try:
            result = await self.slack_service.create_channel(
                community_id=request.community_id,
                name=request.name,
                is_private=request.is_private
            )

            if slack_action_pb2:
                return slack_action_pb2.CreateChannelResponse(
                    success=result['success'],
                    channel_id=result['channel_id'] or '',
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"CreateChannel error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if slack_action_pb2:
                return slack_action_pb2.CreateChannelResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def InviteToChannel(self, request, context):
        """Handle InviteToChannel gRPC call"""
        try:
            result = await self.slack_service.invite_to_channel(
                community_id=request.community_id,
                channel_id=request.channel_id,
                user_ids=list(request.user_ids)
            )

            if slack_action_pb2:
                return slack_action_pb2.InviteToChannelResponse(
                    success=result['success'],
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"InviteToChannel error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if slack_action_pb2:
                return slack_action_pb2.InviteToChannelResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def KickFromChannel(self, request, context):
        """Handle KickFromChannel gRPC call"""
        try:
            result = await self.slack_service.kick_from_channel(
                community_id=request.community_id,
                channel_id=request.channel_id,
                user_id=request.user_id
            )

            if slack_action_pb2:
                return slack_action_pb2.KickFromChannelResponse(
                    success=result['success'],
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"KickFromChannel error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if slack_action_pb2:
                return slack_action_pb2.KickFromChannelResponse(
                    success=False,
                    error=str(e)
                )
            return None

    async def SetTopic(self, request, context):
        """Handle SetTopic gRPC call"""
        try:
            result = await self.slack_service.set_topic(
                community_id=request.community_id,
                channel_id=request.channel_id,
                topic=request.topic
            )

            if slack_action_pb2:
                return slack_action_pb2.SetTopicResponse(
                    success=result['success'],
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"SetTopic error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if slack_action_pb2:
                return slack_action_pb2.SetTopicResponse(
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

            result = await self.slack_service.open_modal(
                community_id=request.community_id,
                trigger_id=request.trigger_id,
                view=view
            )

            if slack_action_pb2:
                return slack_action_pb2.OpenModalResponse(
                    success=result['success'],
                    view_id=result['view_id'] or '',
                    error=result['error'] or ''
                )
            return None

        except Exception as e:
            logger.error(f"OpenModal error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            if slack_action_pb2:
                return slack_action_pb2.OpenModalResponse(
                    success=False,
                    error=str(e)
                )
            return None


def create_grpc_server(slack_service: SlackService, port: int, max_workers: int = 10):
    """
    Create and configure gRPC server

    Args:
        slack_service: Slack service instance
        port: gRPC server port
        max_workers: Maximum number of worker threads

    Returns:
        Configured gRPC server instance
    """
    if not slack_action_pb2_grpc:
        logger.error("Proto files not generated. Run: python -m grpc_tools.protoc")
        return None

    server = grpc.aio.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        interceptors=[AuthInterceptor()],
        options=default_server_options(),
    )

    servicer = SlackActionServicer(slack_service)
    slack_action_pb2_grpc.add_SlackActionServiceServicer_to_server(servicer, server)

    bind_secure_port(server, f'[::]:{port}')

    logger.info(f"gRPC server (TLS) configured on port {port}")
    return server
