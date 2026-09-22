"""
gRPC handler for receiving action tasks from processor/router.
"""
import asyncio
import logging
import uuid
from datetime import datetime
from typing import Dict, Any

import grpc
from concurrent import futures

# Import generated protobuf classes (will be generated from proto file)
# from proto import twitch_action_pb2, twitch_action_pb2_grpc

from services.twitch_service import TwitchService
from services.grpc_auth_interceptor import require_auth

logger = logging.getLogger(__name__)


class TwitchActionServicer:
    """gRPC servicer for Twitch actions."""

    def __init__(self, twitch_service: TwitchService):
        """Initialize gRPC servicer."""
        self.twitch_service = twitch_service

        # Action type mapping
        self.action_handlers = {
            "chat_message": self._handle_chat_message,
            "whisper": self._handle_whisper,
            "announcement": self._handle_announcement,
            "clip": self._handle_clip,
            "poll_create": self._handle_poll_create,
            "poll_end": self._handle_poll_end,
            "prediction_create": self._handle_prediction_create,
            "prediction_resolve": self._handle_prediction_resolve,
            "ban": self._handle_ban,
            "unban": self._handle_unban,
            "timeout": self._handle_timeout,
            "delete_message": self._handle_delete_message,
            "update_title": self._handle_update_title,
            "update_game": self._handle_update_game,
            "marker": self._handle_marker,
            "raid": self._handle_raid,
            "vip_add": self._handle_vip_add,
            "vip_remove": self._handle_vip_remove,
            "mod_add": self._handle_mod_add,
            "mod_remove": self._handle_mod_remove,
        }

    async def ExecuteAction(self, request, context):
        """
        Execute single Twitch action.

        Args:
            request: ActionRequest proto message
            context: gRPC context

        Returns:
            ActionResponse proto message
        """
        await require_auth(context)

        action_id = str(uuid.uuid4())
        action_type = request.action_type
        broadcaster_id = request.broadcaster_id
        parameters = dict(request.parameters)

        logger.info(
            f"Executing action {action_id}: type={action_type}, "
            f"broadcaster={broadcaster_id}"
        )

        try:
            # Get handler for action type
            handler = self.action_handlers.get(action_type)
            if not handler:
                error_msg = f"Unknown action type: {action_type}"
                logger.error(error_msg)
                return self._create_error_response(action_id, error_msg)

            # Execute action
            result = await handler(broadcaster_id, parameters)

            # Create success response
            response = {
                "success": True,
                "message": f"Action {action_type} executed successfully",
                "action_id": action_id,
                "result_data": result or {},
                "error": ""
            }

            logger.info(f"Action {action_id} completed successfully")
            return response

        except Exception as e:
            error_msg = f"Action execution failed: {str(e)}"
            logger.error(f"Action {action_id} failed: {e}", exc_info=True)
            return self._create_error_response(action_id, error_msg)

    async def BatchExecuteActions(self, request, context):
        """
        Execute batch of Twitch actions.

        Args:
            request: BatchActionRequest proto message
            context: gRPC context

        Returns:
            BatchActionResponse proto message
        """
        await require_auth(context)

        actions = request.actions
        total_count = len(actions)

        logger.info(f"Executing batch of {total_count} actions")

        # Execute all actions concurrently
        tasks = [
            self.ExecuteAction(action, context)
            for action in actions
        ]

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        # Count successes and failures
        success_count = sum(1 for r in responses if not isinstance(r, Exception) and r.get("success"))
        failure_count = total_count - success_count

        logger.info(
            f"Batch execution complete: {success_count} succeeded, "
            f"{failure_count} failed"
        )

        return {
            "responses": responses,
            "total_count": total_count,
            "success_count": success_count,
            "failure_count": failure_count
        }

    async def GetActionStatus(self, request, context):
        """
        Get status of action (placeholder for future enhancement).

        Args:
            request: StatusRequest proto message
            context: gRPC context

        Returns:
            StatusResponse proto message
        """
        await require_auth(context)

        action_id = request.action_id

        # For now, just return completed status
        # In future, could track action status in database
        return {
            "action_id": action_id,
            "status": "completed",
            "message": "Action status tracking not yet implemented",
            "metadata": {}
        }

    # Action Handlers

    async def _handle_chat_message(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle chat message action."""
        message = params.get("message", "")
        if not message:
            raise ValueError("Message is required")

        result = await self.twitch_service.send_chat_message(broadcaster_id, message)
        return {"message_sent": message}

    async def _handle_whisper(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle whisper action."""
        to_user_id = params.get("to_user_id", "")
        message = params.get("message", "")

        if not to_user_id or not message:
            raise ValueError("to_user_id and message are required")

        result = await self.twitch_service.send_whisper(broadcaster_id, to_user_id, message)
        return {"whisper_sent": True}

    async def _handle_announcement(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle announcement action."""
        message = params.get("message", "")
        color = params.get("color", "primary")

        if not message:
            raise ValueError("Message is required")

        result = await self.twitch_service.send_announcement(broadcaster_id, message, color)
        return {"announcement_sent": message}

    async def _handle_clip(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle clip creation action."""
        result = await self.twitch_service.create_clip(broadcaster_id)
        return result

    async def _handle_poll_create(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle poll creation action."""
        title = params.get("title", "")
        choices = params.get("choices", "").split(",")
        duration = int(params.get("duration", "60"))

        if not title or not choices:
            raise ValueError("Title and choices are required")

        result = await self.twitch_service.create_poll(broadcaster_id, title, choices, duration)
        return result

    async def _handle_poll_end(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle poll end action."""
        poll_id = params.get("poll_id", "")
        status = params.get("status", "TERMINATED")

        if not poll_id:
            raise ValueError("poll_id is required")

        result = await self.twitch_service.end_poll(broadcaster_id, poll_id, status)
        return result

    async def _handle_prediction_create(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle prediction creation action."""
        title = params.get("title", "")
        outcomes = params.get("outcomes", "").split(",")
        duration = int(params.get("duration", "60"))

        if not title or len(outcomes) != 2:
            raise ValueError("Title and exactly 2 outcomes are required")

        result = await self.twitch_service.create_prediction(broadcaster_id, title, outcomes, duration)
        return result

    async def _handle_prediction_resolve(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle prediction resolution action."""
        prediction_id = params.get("prediction_id", "")
        winning_outcome_id = params.get("winning_outcome_id", "")

        if not prediction_id or not winning_outcome_id:
            raise ValueError("prediction_id and winning_outcome_id are required")

        result = await self.twitch_service.resolve_prediction(
            broadcaster_id,
            prediction_id,
            winning_outcome_id
        )
        return result

    async def _handle_ban(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle ban action."""
        user_id = params.get("user_id", "")
        reason = params.get("reason", "Banned by bot")

        if not user_id:
            raise ValueError("user_id is required")

        result = await self.twitch_service.ban_user(broadcaster_id, user_id, reason)
        return {"user_banned": user_id}

    async def _handle_unban(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle unban action."""
        user_id = params.get("user_id", "")

        if not user_id:
            raise ValueError("user_id is required")

        result = await self.twitch_service.unban_user(broadcaster_id, user_id)
        return {"user_unbanned": user_id}

    async def _handle_timeout(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle timeout action."""
        user_id = params.get("user_id", "")
        duration = int(params.get("duration", "600"))
        reason = params.get("reason", "Timed out by bot")

        if not user_id:
            raise ValueError("user_id is required")

        result = await self.twitch_service.ban_user(broadcaster_id, user_id, reason, duration)
        return {"user_timed_out": user_id, "duration": duration}

    async def _handle_delete_message(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle message deletion action."""
        message_id = params.get("message_id", "")

        if not message_id:
            raise ValueError("message_id is required")

        result = await self.twitch_service.delete_chat_message(broadcaster_id, message_id)
        return {"message_deleted": message_id}

    async def _handle_update_title(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle stream title update action."""
        title = params.get("title", "")

        if not title:
            raise ValueError("Title is required")

        result = await self.twitch_service.update_stream_title(broadcaster_id, title)
        return {"title_updated": title}

    async def _handle_update_game(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle stream game update action."""
        game_id = params.get("game_id", "")

        if not game_id:
            raise ValueError("game_id is required")

        result = await self.twitch_service.update_stream_game(broadcaster_id, game_id)
        return {"game_updated": game_id}

    async def _handle_marker(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle stream marker creation action."""
        description = params.get("description")
        result = await self.twitch_service.create_stream_marker(broadcaster_id, description)
        return result

    async def _handle_raid(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle raid action."""
        to_broadcaster_id = params.get("to_broadcaster_id", "")

        if not to_broadcaster_id:
            raise ValueError("to_broadcaster_id is required")

        result = await self.twitch_service.raid(broadcaster_id, to_broadcaster_id)
        return {"raid_started": to_broadcaster_id}

    async def _handle_vip_add(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle VIP add action."""
        user_id = params.get("user_id", "")

        if not user_id:
            raise ValueError("user_id is required")

        result = await self.twitch_service.manage_vip(broadcaster_id, user_id, "add")
        return {"vip_added": user_id}

    async def _handle_vip_remove(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle VIP remove action."""
        user_id = params.get("user_id", "")

        if not user_id:
            raise ValueError("user_id is required")

        result = await self.twitch_service.manage_vip(broadcaster_id, user_id, "remove")
        return {"vip_removed": user_id}

    async def _handle_mod_add(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle moderator add action."""
        user_id = params.get("user_id", "")

        if not user_id:
            raise ValueError("user_id is required")

        result = await self.twitch_service.manage_moderator(broadcaster_id, user_id, "add")
        return {"moderator_added": user_id}

    async def _handle_mod_remove(self, broadcaster_id: str, params: Dict) -> Dict:
        """Handle moderator remove action."""
        user_id = params.get("user_id", "")

        if not user_id:
            raise ValueError("user_id is required")

        result = await self.twitch_service.manage_moderator(broadcaster_id, user_id, "remove")
        return {"moderator_removed": user_id}

    def _create_error_response(self, action_id: str, error_msg: str) -> Dict:
        """Create error response."""
        return {
            "success": False,
            "message": "Action execution failed",
            "action_id": action_id,
            "result_data": {},
            "error": error_msg
        }


class GrpcServer:
    """gRPC server wrapper."""

    def __init__(self, servicer: TwitchActionServicer, port: int):
        """Initialize gRPC server."""
        self.servicer = servicer
        self.port = port
        self.server = None

    async def start(self):
        """Start gRPC server."""
        # Note: Actual gRPC server implementation would use generated protobuf code
        # This is a placeholder structure
        logger.info(f"Starting gRPC server on port {self.port}")

        # In production, would be:
        # from services.grpc_tls import bind_secure_port, default_server_options
        # self.server = grpc.aio.server(options=default_server_options())
        # twitch_action_pb2_grpc.add_TwitchActionServiceServicer_to_server(
        #     self.servicer, self.server
        # )
        # bind_secure_port(self.server, f'[::]:{self.port}')  # TLS -- never add_insecure_port
        # await self.server.start()

        logger.info(f"gRPC server started on port {self.port}")

    async def stop(self):
        """Stop gRPC server."""
        if self.server:
            logger.info("Stopping gRPC server")
            await self.server.stop(grace=5)
            logger.info("gRPC server stopped")
