"""
Example Usage of ModeController Service

Demonstrates how to use the ModeController to manage switching between Music Player
and Radio Player modes, including initialization, mode switching, and state management.
"""

import asyncio
import logging
from mode_controller import ModeController, PlayMode, create_mode_controller

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def example_basic_mode_switching():
    """Example 1: Basic mode switching between music and radio"""
    logger.info("=== Example 1: Basic Mode Switching ===")

    # Create mock player instances (in real code, these would be actual MusicPlayer and RadioPlayer)
    class MockMusicPlayer:
        async def get_playback_state(self, community_id):
            return None

        async def pause(self, community_id):
            return True

        async def stop_playback(self, community_id):
            return True

    class MockRadioPlayer:
        async def get_current_station(self, community_id):
            return None

        async def stop_station(self, community_id):
            return True

    music_player = MockMusicPlayer()
    radio_player = MockRadioPlayer()

    # Create mode controller
    mode_controller = create_mode_controller(
        music_player=music_player,
        radio_player=radio_player,
        browser_source_url="http://localhost:8050"
    )

    await mode_controller.initialize()

    try:
        community_id = 12345

        # Switch to music mode
        logger.info("Switching to music mode...")
        success = await mode_controller.switch_to_music(community_id)
        logger.info(f"Switch to music: {success}")

        mode = await mode_controller.get_active_mode(community_id)
        logger.info(f"Active mode: {mode}")

        # Switch to radio mode
        logger.info("Switching to radio mode...")
        success = await mode_controller.switch_to_radio(community_id)
        logger.info(f"Switch to radio: {success}")

        mode = await mode_controller.get_active_mode(community_id)
        logger.info(f"Active mode: {mode}")

        # Switch back to music mode
        logger.info("Switching back to music mode...")
        success = await mode_controller.switch_to_music(community_id)
        logger.info(f"Switch to music: {success}")

        mode = await mode_controller.get_active_mode(community_id)
        logger.info(f"Active mode: {mode}")

    finally:
        await mode_controller.shutdown()


async def example_mode_state_tracking():
    """Example 2: Tracking mode state across multiple communities"""
    logger.info("\n=== Example 2: Mode State Tracking ===")

    class MockMusicPlayer:
        async def get_playback_state(self, community_id):
            return None

        async def pause(self, community_id):
            return True

        async def resume(self, community_id):
            return True

        async def stop_playback(self, community_id):
            return True

    class MockRadioPlayer:
        async def get_current_station(self, community_id):
            return None

        async def stop_station(self, community_id):
            return True

    music_player = MockMusicPlayer()
    radio_player = MockRadioPlayer()

    mode_controller = create_mode_controller(
        music_player=music_player,
        radio_player=radio_player
    )

    await mode_controller.initialize()

    try:
        # Set up multiple communities with different modes
        communities = [101, 102, 103]

        for i, community_id in enumerate(communities):
            if i % 2 == 0:
                await mode_controller.switch_to_music(community_id)
                logger.info(f"Community {community_id}: Music mode")
            else:
                await mode_controller.switch_to_radio(community_id)
                logger.info(f"Community {community_id}: Radio mode")

        # Get all mode states
        all_states = await mode_controller.get_all_mode_states()
        logger.info(f"\nAll mode states ({len(all_states)} communities):")
        for community_id, state in all_states.items():
            logger.info(f"  Community {community_id}: {state.active_mode.value}")

    finally:
        await mode_controller.shutdown()


async def example_mode_state_dict():
    """Example 3: Serializing mode state to dictionary"""
    logger.info("\n=== Example 3: Mode State Dictionary Serialization ===")

    class MockMusicPlayer:
        async def get_playback_state(self, community_id):
            return None

        async def pause(self, community_id):
            return True

        async def stop_playback(self, community_id):
            return True

    class MockRadioPlayer:
        async def get_current_station(self, community_id):
            return None

        async def stop_station(self, community_id):
            return True

    music_player = MockMusicPlayer()
    radio_player = MockRadioPlayer()

    mode_controller = create_mode_controller(
        music_player=music_player,
        radio_player=radio_player
    )

    await mode_controller.initialize()

    try:
        community_id = 54321

        # Switch to music mode
        await mode_controller.switch_to_music(community_id)

        # Get mode state and convert to dict
        mode_state = await mode_controller.get_mode_state(community_id)
        if mode_state:
            state_dict = mode_state.to_dict()
            logger.info("Mode state as dictionary:")
            for key, value in state_dict.items():
                logger.info(f"  {key}: {value}")

    finally:
        await mode_controller.shutdown()


async def example_stop_playback():
    """Example 4: Stopping all playback"""
    logger.info("\n=== Example 4: Stopping Playback ===")

    class MockMusicPlayer:
        async def get_playback_state(self, community_id):
            return None

        async def pause(self, community_id):
            return True

        async def stop_playback(self, community_id):
            logger.info(f"[MockMusicPlayer] Stopping music playback for community {community_id}")
            return True

    class MockRadioPlayer:
        async def get_current_station(self, community_id):
            return None

        async def stop_station(self, community_id):
            logger.info(f"[MockRadioPlayer] Stopping radio station for community {community_id}")
            return True

    music_player = MockMusicPlayer()
    radio_player = MockRadioPlayer()

    mode_controller = create_mode_controller(
        music_player=music_player,
        radio_player=radio_player
    )

    await mode_controller.initialize()

    try:
        community_id = 99999

        # Activate music mode
        await mode_controller.switch_to_music(community_id)
        logger.info(f"Active mode: {await mode_controller.get_active_mode(community_id)}")

        # Stop playback
        logger.info("Stopping all playback...")
        success = await mode_controller.stop_current_mode(community_id)
        logger.info(f"Stopped successfully: {success}")

        mode = await mode_controller.get_active_mode(community_id)
        logger.info(f"Active mode after stop: {mode}")

    finally:
        await mode_controller.shutdown()


async def main():
    """Run all examples"""
    logger.info("Starting ModeController Usage Examples\n")

    try:
        await example_basic_mode_switching()
        await example_mode_state_tracking()
        await example_mode_state_dict()
        await example_stop_playback()

        logger.info("\n\nAll examples completed successfully!")

    except Exception as e:
        logger.error(f"Error running examples: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
