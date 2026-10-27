"""
Quartermaster Inventory Service - Practical Examples

This file demonstrates real-world usage patterns for the InventoryService.
These examples show complete workflows for managing community inventory.
"""

import asyncio
from datetime import datetime, timedelta


class InventoryExamples:
    """Collection of practical inventory service examples"""

    def __init__(self, inventory_service):
        """
        Initialize examples with service instance.

        Args:
            inventory_service: InventoryService instance
        """
        self.service = inventory_service

    # =========================================================================
    # EXAMPLE 1: Set Up Community Inventory
    # =========================================================================

    async def setup_new_community_inventory(self, community_id: int):
        """
        Set up initial inventory for a new community.
        Creates a sample collection of items across multiple categories.

        Args:
            community_id: Community ID to set up inventory for
        """
        print(f"\n=== Setting up inventory for community {community_id} ===\n")

        items_to_create = [
            {
                'name': 'Gaming Laptop',
                'description': 'High-performance laptop for gaming events',
                'item_type': 'equipment',
                'category': 'electronics',
                'quantity': 2,
                'checkout_price': 100,
                'max_checkout_duration_hours': 72,
                'image_url': 'https://example.com/gaming-laptop.jpg'
            },
            {
                'name': 'Streaming Microphone',
                'description': 'Professional quality microphone for streaming',
                'item_type': 'equipment',
                'category': 'audio',
                'quantity': 5,
                'checkout_price': 25,
                'max_checkout_duration_hours': 48,
                'image_url': 'https://example.com/microphone.jpg'
            },
            {
                'name': 'Ring Light',
                'description': '10-inch LED ring light for streaming',
                'item_type': 'equipment',
                'category': 'lighting',
                'quantity': 3,
                'checkout_price': 15,
                'max_checkout_duration_hours': 48,
                'image_url': 'https://example.com/ring-light.jpg'
            },
            {
                'name': 'Board Games',
                'description': 'Collection of strategy board games',
                'item_type': 'collectible',
                'category': 'games',
                'quantity': 10,
                'checkout_price': 0,  # Free
                'max_checkout_duration_hours': 168,  # 1 week
                'image_url': 'https://example.com/board-games.jpg'
            },
            {
                'name': 'Portable Speaker',
                'description': 'Bluetooth portable speaker',
                'item_type': 'equipment',
                'category': 'audio',
                'quantity': 4,
                'checkout_price': 20,
                'max_checkout_duration_hours': 48,
                'image_url': 'https://example.com/speaker.jpg'
            },
        ]

        created_items = []
        for item_data in items_to_create:
            try:
                item = await self.service.add_item(
                    community_id=community_id,
                    created_by_user_id=1,  # Admin user
                    **item_data
                )
                created_items.append(item)
                print(f"✓ Created: {item['name']} (ID: {item['id']})")
                print(f"  Category: {item['category']}, Quantity: {item['quantity']}")
            except Exception as e:
                print(f"✗ Failed to create item: {e}")

        return created_items

    # =========================================================================
    # EXAMPLE 2: Member Checkout Workflow
    # =========================================================================

    async def member_checkout_workflow(
        self,
        community_id: int,
        item_id: int,
        user_id: int
    ):
        """
        Complete workflow for a member checking out an item.

        Args:
            community_id: Community ID
            item_id: Item to checkout
            user_id: User checking out the item
        """
        print("\n=== Checkout Workflow ===\n")

        try:
            # Step 1: Check item availability
            print("Step 1: Checking item availability...")
            item = await self.service.get_item(community_id, item_id)

            if not item:
                print("✗ Item not found")
                return

            print(f"✓ Item: {item['name']}")
            print(f"  Available: {item['available_quantity']}/{item['quantity']}")
            print(f"  Checkout price: {item['checkout_price']} currency units")

            if item['available_quantity'] <= 0:
                print("✗ Item not available for checkout")
                return

            # Step 2: Process checkout
            print("\nStep 2: Processing checkout...")
            checkout = await self.service.checkout_item(
                community_id=community_id,
                item_id=item_id,
                user_id=user_id,
                quantity=1,
                checkout_duration_hours=item['max_checkout_duration_hours'],
                notes="Checked out by member"
            )

            print(f"✓ Checkout successful (ID: {checkout['id']})")
            print(f"  Checked out at: {checkout['checked_out_at']}")
            print(f"  Due at: {checkout['due_at']}")

            # Step 3: Verify inventory updated
            print("\nStep 3: Verifying inventory...")
            updated_item = await self.service.get_item(community_id, item_id)
            print(f"✓ Available quantity updated: {updated_item['available_quantity']}")

            return checkout

        except Exception as e:
            print(f"✗ Checkout failed: {e}")
            return None

    # =========================================================================
    # EXAMPLE 3: Item Return and Inspection
    # =========================================================================

    async def member_return_workflow(
        self,
        community_id: int,
        checkout_id: int,
        returned_condition: str
    ):
        """
        Workflow for a member returning an item.

        Args:
            community_id: Community ID
            checkout_id: Checkout ID to return
            returned_condition: Condition of returned item
        """
        print("\n=== Return Workflow ===\n")

        try:
            # Process return
            print(f"Processing return for checkout {checkout_id}...")
            returned = await self.service.checkin_item(
                community_id=community_id,
                checkout_id=checkout_id,
                returned_condition=returned_condition,
                returned_by_user_id=None  # Use checkout user
            )

            print("✓ Return processed successfully")
            print(f"  Status: {returned['status']}")
            print(f"  Returned at: {returned['returned_at']}")
            print(f"  Item ID: {returned['item_id']}")

            # Get updated item info
            item = await self.service.get_item(community_id, returned['item_id'])
            print(f"  Item available quantity: {item['available_quantity']}")

            return returned

        except Exception as e:
            print(f"✗ Return failed: {e}")
            return None

    # =========================================================================
    # EXAMPLE 4: Search and Discovery
    # =========================================================================

    async def search_inventory(
        self,
        community_id: int,
        search_query: str
    ):
        """
        Search for items in inventory using full-text search.

        Args:
            community_id: Community ID
            search_query: Search query
        """
        print(f"\n=== Search Results for '{search_query}' ===\n")

        try:
            results = await self.service.search_items(
                community_id=community_id,
                query=search_query,
                limit=20
            )

            if not results:
                print("No items found matching search query")
                return

            print(f"Found {len(results)} items:\n")
            for item in results:
                availability = "✓ Available" if item['available_quantity'] > 0 else "✗ Unavailable"
                print(f"- {item['name']} ({item['category']})")
                print(f"  {availability}: {item['available_quantity']}/{item['quantity']}")
                print(f"  Checkout price: {item['checkout_price']}")
                print()

            return results

        except Exception as e:
            print(f"✗ Search failed: {e}")
            return None

    # =========================================================================
    # EXAMPLE 5: Category Browse
    # =========================================================================

    async def browse_category(
        self,
        community_id: int,
        category: str
    ):
        """
        Browse items by category.

        Args:
            community_id: Community ID
            category: Category to browse
        """
        print(f"\n=== Browsing Category: {category} ===\n")

        try:
            items = await self.service.get_items_by_category(
                community_id=community_id,
                category=category,
                include_unavailable=False  # Only show available items
            )

            if not items:
                print(f"No available items in '{category}' category")
                return

            print(f"Available items in '{category}':\n")
            for item in items:
                print(f"• {item['name']}")
                print(f"  Available: {item['available_quantity']}/{item['quantity']}")
                print(f"  Price: {item['checkout_price']} currency units")
                if item['description']:
                    print(f"  {item['description']}")
                print()

            return items

        except Exception as e:
            print(f"✗ Browse failed: {e}")
            return None

    # =========================================================================
    # EXAMPLE 6: Inventory Management Dashboard
    # =========================================================================

    async def generate_inventory_report(self, community_id: int):
        """
        Generate comprehensive inventory report for community.

        Args:
            community_id: Community ID
        """
        print(f"\n=== Inventory Report for Community {community_id} ===\n")

        try:
            # Get summary
            summary = await self.service.get_inventory_summary(community_id)

            print("INVENTORY SUMMARY")
            print("-" * 40)
            print(f"Total items: {summary['total_items']}")
            print(f"Total quantity: {summary['total_quantity']}")
            print(f"Available: {summary['total_available']}")
            print(f"Checked out: {summary['total_quantity'] - summary['total_available']}")
            print()

            print("CHECKOUT STATUS")
            print("-" * 40)
            print(f"Active checkouts: {summary['active_checkouts']}")
            print(f"Overdue checkouts: {summary['overdue_checkouts']}")
            print()

            print("INVENTORY ALERTS")
            print("-" * 40)
            print(f"Low stock items: {summary['low_stock_items']}")

            if summary['low_stock_items'] > 0:
                low_stock = await self.service.get_low_stock_items(community_id)
                print("\nLow Stock Items:")
                for item in low_stock[:5]:
                    checked_out = item['quantity'] - item['available_quantity']
                    print(f"  - {item['name']}: {checked_out} checked out, "
                          f"{item['available_quantity']} remaining")

            if summary['overdue_checkouts'] > 0:
                overdue = await self.service.get_overdue_checkouts(community_id)
                print("\nOverdue Checkouts:")
                for checkout in overdue[:5]:
                    print(f"  - Item {checkout['item_id']}: "
                          f"Due {checkout['due_at']} (User {checkout['user_id']})")

            return summary

        except Exception as e:
            print(f"✗ Report generation failed: {e}")
            return None

    # =========================================================================
    # EXAMPLE 7: Restocking
    # =========================================================================

    async def restock_item(
        self,
        community_id: int,
        item_id: int,
        quantity: int,
        admin_user_id: int
    ):
        """
        Restock an inventory item.

        Args:
            community_id: Community ID
            item_id: Item to restock
            quantity: Quantity to add
            admin_user_id: Admin user performing restock
        """
        print("\n=== Restocking Item ===\n")

        try:
            # Get current item
            print("Fetching current item status...")
            item = await self.service.get_item(community_id, item_id)

            if not item:
                print("✗ Item not found")
                return

            print(f"Item: {item['name']}")
            print(f"Current quantity: {item['quantity']}")
            print(f"Available: {item['available_quantity']}")

            # Add stock
            print(f"\nAdding {quantity} units...")
            updated = await self.service.add_stock(
                community_id=community_id,
                item_id=item_id,
                quantity=quantity,
                reason="Restocking",
                added_by_user_id=admin_user_id
            )

            print("✓ Restock successful")
            print(f"New quantity: {updated['quantity']}")
            print(f"New available: {updated['available_quantity']}")

            return updated

        except Exception as e:
            print(f"✗ Restock failed: {e}")
            return None

    # =========================================================================
    # EXAMPLE 8: User Checkout History
    # =========================================================================

    async def view_user_history(
        self,
        community_id: int,
        user_id: int
    ):
        """
        View checkout history for a user.

        Args:
            community_id: Community ID
            user_id: User ID
        """
        print(f"\n=== Checkout History for User {user_id} ===\n")

        try:
            # Get all checkouts
            all_checkouts = await self.service.get_user_checkouts(
                community_id=community_id,
                user_id=user_id
            )

            if not all_checkouts:
                print("No checkout history found")
                return

            # Separate by status
            active = [c for c in all_checkouts if c['status'] == 'active']
            returned = [c for c in all_checkouts if c['status'] == 'returned']

            print(f"Total checkouts: {len(all_checkouts)}")
            print(f"Active: {len(active)}")
            print(f"Returned: {len(returned)}")
            print()

            if active:
                print("ACTIVE CHECKOUTS:")
                print("-" * 40)
                for checkout in active:
                    print(f"Item {checkout['item_id']}")
                    print(f"  Checked out: {checkout['checked_out_at']}")
                    print(f"  Due: {checkout['due_at']}")
                    print(f"  Quantity: {checkout['quantity']}")
                    print()

            if returned:
                print("RETURNED ITEMS:")
                print("-" * 40)
                for checkout in returned[-5:]:  # Last 5 returns
                    print(f"Item {checkout['item_id']}")
                    print(f"  Returned: {checkout['returned_at']}")
                    print()

            return all_checkouts

        except Exception as e:
            print(f"✗ History retrieval failed: {e}")
            return None

    # =========================================================================
    # EXAMPLE 9: Audit Trail Review
    # =========================================================================

    async def review_audit_log(
        self,
        community_id: int,
        action: str = None
    ):
        """
        Review audit log for inventory operations.

        Args:
            community_id: Community ID
            action: Optional specific action to filter
        """
        print("\n=== Audit Log ===\n")

        try:
            logs = await self.service.get_audit_log(
                community_id=community_id,
                action=action,
                limit=20
            )

            if not logs:
                print("No audit log entries found")
                return

            print(f"Recent operations ({len(logs)} entries):\n")
            for log in logs:
                action_type = log['action'].upper()
                item_id = log['item_id'] or 'N/A'
                user_id = log['user_id'] or 'System'
                quantity = log['quantity_change'] or ''

                print(f"[{log['created_at']}] {action_type}")
                print(f"  Item ID: {item_id}")
                print(f"  User: {user_id}")
                if quantity:
                    print(f"  Quantity change: {quantity:+d}")
                if log['details']:
                    print(f"  Details: {log['details']}")
                print()

            return logs

        except Exception as e:
            print(f"✗ Audit log retrieval failed: {e}")
            return None

    # =========================================================================
    # EXAMPLE 10: Complete E2E Test
    # =========================================================================

    async def run_complete_workflow(self, community_id: int):
        """
        Run a complete end-to-end inventory workflow.

        Args:
            community_id: Community ID for testing
        """
        print("\n" + "=" * 50)
        print("COMPLETE INVENTORY WORKFLOW TEST")
        print("=" * 50)

        try:
            # 1. Setup inventory
            print("\n[1/7] Setting up inventory...")
            items = await self.setup_new_community_inventory(community_id)
            if not items:
                print("✗ Setup failed")
                return

            item_id = items[0]['id']

            # 2. View summary
            print("\n[2/7] Viewing inventory summary...")
            await self.generate_inventory_report(community_id)

            # 3. Search items
            print("\n[3/7] Searching for items...")
            await self.search_inventory(community_id, "laptop")

            # 4. Browse category
            print("\n[4/7] Browsing category...")
            await self.browse_category(community_id, "electronics")

            # 5. Checkout item
            print("\n[5/7] Processing checkout...")
            checkout = await self.member_checkout_workflow(
                community_id, item_id, user_id=100
            )
            if not checkout:
                print("✗ Checkout failed")
                return

            checkout_id = checkout['id']

            # 6. View user history
            print("\n[6/7] Viewing user history...")
            await self.view_user_history(community_id, 100)

            # 7. Return item
            print("\n[7/7] Processing return...")
            await self.member_return_workflow(
                community_id, checkout_id, "Good condition"
            )

            # Final summary
            print("\n[COMPLETE] Final inventory summary...")
            await self.generate_inventory_report(community_id)

            print("\n" + "=" * 50)
            print("✓ WORKFLOW TEST COMPLETED SUCCESSFULLY")
            print("=" * 50 + "\n")

        except Exception as e:
            print(f"\n✗ WORKFLOW TEST FAILED: {e}\n")


# ============================================================================
# MAIN - Run Examples
# ============================================================================

async def main():
    """
    Run all examples.
    Note: Requires initialized InventoryService instance.
    """
    # This would be initialized with your AsyncDAL instance
    # from flask_core import init_database
    # from action.interactive.inventory_interaction_module.services import InventoryService
    #
    # dal = init_database(uri="postgresql://...")
    # service = InventoryService(dal)
    #
    # examples = InventoryExamples(service)
    # await examples.run_complete_workflow(community_id=1)

    print("Examples module loaded. Create InventoryExamples instance with service.")


if __name__ == "__main__":
    asyncio.run(main())
