"""
Quartermaster Inventory Service

Comprehensive inventory management system supporting item CRUD operations,
checkout/checkin workflows with due dates, full-text search, and audit logging.
Uses AsyncDAL pattern for non-blocking database operations.
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class InventoryService:
    """
    Service for managing community inventory items.

    Features:
    - CRUD operations for inventory items
    - Checkout/checkin functionality with due dates
    - Full-text search with PostgreSQL GIN index
    - Category and availability filtering
    - Comprehensive audit logging
    - Optional integration with community currency for checkout pricing
    """

    def __init__(self, dal):
        """
        Initialize inventory service.

        Args:
            dal: AsyncDAL database access layer instance
        """
        self.dal = dal

    # =========================================================================
    # ITEM CRUD OPERATIONS
    # =========================================================================

    async def add_item(
        self,
        community_id: int,
        name: str,
        description: Optional[str] = None,
        item_type: Optional[str] = None,
        category: Optional[str] = None,
        quantity: int = 0,
        checkout_price: int = 0,
        max_checkout_duration_hours: Optional[int] = None,
        image_url: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_by_user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Add a new inventory item to the community.

        Args:
            community_id: Community ID
            name: Item name
            description: Item description
            item_type: Type of item (equipment, consumable, collectible, etc.)
            category: Category for organization
            quantity: Initial quantity in inventory
            checkout_price: Cost in community currency to checkout (0 = free)
            max_checkout_duration_hours: Maximum checkout duration in hours
            image_url: URL to item image/thumbnail
            metadata: Additional custom fields (JSON)
            created_by_user_id: User ID who created the item

        Returns:
            Dictionary with item details including ID
        """
        try:
            # Validate quantity
            if quantity < 0:
                raise ValueError("Quantity cannot be negative")

            # Prepare metadata
            metadata = metadata or {}

            sql = """
                INSERT INTO inventory_items
                (community_id, name, description, item_type, category,
                 quantity, available_quantity, checkout_price,
                 max_checkout_duration_hours, image_url, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING id, created_at, updated_at
            """

            result = await self.dal.execute(
                sql,
                [
                    community_id,
                    name,
                    description,
                    item_type,
                    category,
                    quantity,
                    quantity,  # available_quantity = quantity initially
                    checkout_price,
                    max_checkout_duration_hours,
                    image_url,
                    metadata
                ]
            )

            if not result:
                raise Exception("Failed to insert inventory item")

            item_id = result[0]['id']

            # Log the action
            await self._log_action(
                action='add_stock',
                item_id=item_id,
                community_id=community_id,
                user_id=created_by_user_id,
                quantity_change=quantity,
                details={
                    'name': name,
                    'category': category,
                    'item_type': item_type,
                    'reason': 'Item creation'
                }
            )

            logger.info(
                f"Item {item_id} ({name}) added to community {community_id} "
                f"with quantity {quantity}"
            )

            return {
                'id': item_id,
                'community_id': community_id,
                'name': name,
                'description': description,
                'item_type': item_type,
                'category': category,
                'quantity': quantity,
                'available_quantity': quantity,
                'checkout_price': checkout_price,
                'max_checkout_duration_hours': max_checkout_duration_hours,
                'image_url': image_url,
                'metadata': metadata,
                'created_at': result[0]['created_at'],
                'updated_at': result[0]['updated_at']
            }

        except Exception as e:
            logger.error(f"Failed to add inventory item: {e}")
            raise

    async def get_item(self, community_id: int, item_id: int) -> Optional[Dict[str, Any]]:
        """
        Get a specific inventory item by ID.

        Args:
            community_id: Community ID
            item_id: Item ID

        Returns:
            Dictionary with item details or None if not found
        """
        try:
            sql = """
                SELECT id, community_id, name, description, item_type, category,
                       quantity, available_quantity, checkout_price,
                       max_checkout_duration_hours, image_url, metadata,
                       created_at, updated_at
                FROM inventory_items
                WHERE id = $1 AND community_id = $2 AND deleted_at IS NULL
            """

            result = await self.dal.execute(sql, [item_id, community_id])
            return result[0] if result else None

        except Exception as e:
            logger.error(f"Failed to get inventory item {item_id}: {e}")
            raise

    async def update_item(
        self,
        community_id: int,
        item_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
        item_type: Optional[str] = None,
        category: Optional[str] = None,
        checkout_price: Optional[int] = None,
        max_checkout_duration_hours: Optional[int] = None,
        image_url: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        updated_by_user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Update an inventory item.

        Args:
            community_id: Community ID
            item_id: Item ID to update
            name: New item name
            description: New description
            item_type: New item type
            category: New category
            checkout_price: New checkout price
            max_checkout_duration_hours: New max checkout duration
            image_url: New image URL
            metadata: New metadata
            updated_by_user_id: User ID who updated the item

        Returns:
            Updated item dictionary
        """
        try:
            # Get current item
            current_item = await self.get_item(community_id, item_id)
            if not current_item:
                raise ValueError(f"Item {item_id} not found in community {community_id}")

            # Build update query dynamically
            update_fields = []
            params = []
            param_count = 1

            if name is not None:
                update_fields.append(f"name = ${param_count}")
                params.append(name)
                param_count += 1

            if description is not None:
                update_fields.append(f"description = ${param_count}")
                params.append(description)
                param_count += 1

            if item_type is not None:
                update_fields.append(f"item_type = ${param_count}")
                params.append(item_type)
                param_count += 1

            if category is not None:
                update_fields.append(f"category = ${param_count}")
                params.append(category)
                param_count += 1

            if checkout_price is not None:
                update_fields.append(f"checkout_price = ${param_count}")
                params.append(checkout_price)
                param_count += 1

            if max_checkout_duration_hours is not None:
                update_fields.append(f"max_checkout_duration_hours = ${param_count}")
                params.append(max_checkout_duration_hours)
                param_count += 1

            if image_url is not None:
                update_fields.append(f"image_url = ${param_count}")
                params.append(image_url)
                param_count += 1

            if metadata is not None:
                update_fields.append(f"metadata = ${param_count}")
                params.append(metadata)
                param_count += 1

            if not update_fields:
                return current_item

            # Add updated_at timestamp
            update_fields.append("updated_at = NOW()")

            # Add WHERE clause params
            params.append(item_id)
            params.append(community_id)

            sql = f"""
                UPDATE inventory_items
                SET {', '.join(update_fields)}
                WHERE id = ${param_count} AND community_id = ${param_count + 1}
                       AND deleted_at IS NULL
                RETURNING id, community_id, name, description, item_type, category,
                          quantity, available_quantity, checkout_price,
                          max_checkout_duration_hours, image_url, metadata,
                          created_at, updated_at
            """

            result = await self.dal.execute(sql, params)

            if not result:
                raise Exception("Failed to update inventory item")

            # Log the update
            await self._log_action(
                action='update',
                item_id=item_id,
                community_id=community_id,
                user_id=updated_by_user_id,
                details={
                    'old_values': {
                        'name': current_item['name'],
                        'category': current_item['category'],
                        'checkout_price': current_item['checkout_price']
                    },
                    'new_values': {
                        'name': name or current_item['name'],
                        'category': category or current_item['category'],
                        'checkout_price': checkout_price or current_item['checkout_price']
                    }
                }
            )

            logger.info(f"Item {item_id} updated in community {community_id}")

            return result[0]

        except Exception as e:
            logger.error(f"Failed to update inventory item {item_id}: {e}")
            raise

    async def delete_item(
        self,
        community_id: int,
        item_id: int,
        deleted_by_user_id: Optional[int] = None
    ) -> bool:
        """
        Soft delete an inventory item (marks as deleted).

        Args:
            community_id: Community ID
            item_id: Item ID to delete
            deleted_by_user_id: User ID who deleted the item

        Returns:
            True if successful
        """
        try:
            # Verify item exists
            item = await self.get_item(community_id, item_id)
            if not item:
                raise ValueError(f"Item {item_id} not found in community {community_id}")

            sql = """
                UPDATE inventory_items
                SET deleted_at = NOW(), updated_at = NOW()
                WHERE id = $1 AND community_id = $2 AND deleted_at IS NULL
            """

            await self.dal.execute(sql, [item_id, community_id])

            # Log the deletion
            await self._log_action(
                action='delete',
                item_id=item_id,
                community_id=community_id,
                user_id=deleted_by_user_id,
                details={
                    'name': item['name'],
                    'quantity': item['quantity']
                }
            )

            logger.info(f"Item {item_id} deleted in community {community_id}")

            return True

        except Exception as e:
            logger.error(f"Failed to delete inventory item {item_id}: {e}")
            raise

    # =========================================================================
    # CHECKOUT/CHECKIN OPERATIONS
    # =========================================================================

    async def checkout_item(
        self,
        community_id: int,
        item_id: int,
        user_id: int,
        quantity: int = 1,
        checkout_duration_hours: Optional[int] = None,
        notes: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Checkout item(s) from inventory.

        Args:
            community_id: Community ID
            item_id: Item ID to checkout
            user_id: User ID checking out the item
            quantity: Quantity to checkout (default: 1)
            checkout_duration_hours: Duration before due (uses item max if not specified)
            notes: Additional notes about the checkout

        Returns:
            Checkout record dictionary
        """
        try:
            # Validate quantity
            if quantity <= 0:
                raise ValueError("Checkout quantity must be positive")

            # Get item to check availability and duration
            item = await self.get_item(community_id, item_id)
            if not item:
                raise ValueError(f"Item {item_id} not found in community {community_id}")

            if item['available_quantity'] < quantity:
                raise ValueError(
                    f"Insufficient quantity available. "
                    f"Available: {item['available_quantity']}, Requested: {quantity}"
                )

            # Determine due date
            due_at = None
            duration = checkout_duration_hours or item['max_checkout_duration_hours']
            if duration:
                due_at = datetime.utcnow() + timedelta(hours=duration)

            # Use the stored procedure to update inventory
            sql = """
                SELECT * FROM update_inventory_on_checkout($1, $2, $3, $4, $5)
            """

            result = await self.dal.execute(
                sql,
                [item_id, quantity, user_id, community_id, notes]
            )

            if not result or not result[0]['success']:
                raise Exception(
                    result[0]['message'] if result else "Failed to checkout item"
                )

            # Create checkout record
            checkout_sql = """
                INSERT INTO inventory_checkouts
                (item_id, user_id, community_id, quantity, due_at, status, notes)
                VALUES ($1, $2, $3, $4, $5, 'active', $6)
                RETURNING id, checked_out_at, due_at, status
            """

            checkout_result = await self.dal.execute(
                checkout_sql,
                [item_id, user_id, community_id, quantity, due_at, notes]
            )

            if not checkout_result:
                raise Exception("Failed to create checkout record")

            checkout_record = checkout_result[0]

            logger.info(
                f"User {user_id} checked out {quantity} of item {item_id} "
                f"in community {community_id}"
            )

            return {
                'id': checkout_record['id'],
                'item_id': item_id,
                'user_id': user_id,
                'quantity': quantity,
                'checked_out_at': checkout_record['checked_out_at'],
                'due_at': checkout_record['due_at'],
                'status': checkout_record['status'],
                'notes': notes
            }

        except Exception as e:
            logger.error(f"Failed to checkout item {item_id}: {e}")
            raise

    async def checkin_item(
        self,
        community_id: int,
        checkout_id: int,
        quantity: Optional[int] = None,
        returned_condition: Optional[str] = None,
        returned_by_user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Checkin item(s) to inventory.

        Args:
            community_id: Community ID
            checkout_id: Checkout record ID
            quantity: Quantity to return (None = return all)
            returned_condition: Condition of returned item
            returned_by_user_id: User ID processing the return

        Returns:
            Updated checkout record dictionary
        """
        try:
            # Get checkout record
            checkout_sql = """
                SELECT id, item_id, user_id, quantity, status
                FROM inventory_checkouts
                WHERE id = $1 AND community_id = $2 AND status = 'active'
            """

            checkout_result = await self.dal.execute(
                checkout_sql,
                [checkout_id, community_id]
            )

            if not checkout_result:
                raise ValueError(
                    f"Checkout {checkout_id} not found or already processed"
                )

            checkout = checkout_result[0]
            return_qty = quantity or checkout['quantity']

            if return_qty <= 0 or return_qty > checkout['quantity']:
                raise ValueError(
                    f"Invalid return quantity. "
                    f"Checked out: {checkout['quantity']}, Returning: {return_qty}"
                )

            # Use stored procedure to update inventory
            sql = """
                SELECT * FROM update_inventory_on_return($1, $2, $3, $4, $5)
            """

            result = await self.dal.execute(
                sql,
                [
                    checkout['item_id'],
                    return_qty,
                    returned_by_user_id or checkout['user_id'],
                    community_id,
                    returned_condition
                ]
            )

            if not result or not result[0]['success']:
                raise Exception(
                    result[0]['message'] if result else "Failed to return item"
                )

            # Determine new status
            new_status = 'returned' if return_qty == checkout['quantity'] else 'active'

            # Update checkout record
            update_sql = """
                UPDATE inventory_checkouts
                SET returned_at = NOW(),
                    status = $1,
                    notes = CASE WHEN notes IS NULL THEN $2
                                 ELSE notes || ' | ' || $2 END
                WHERE id = $3
                RETURNING id, item_id, user_id, quantity, checked_out_at,
                          due_at, returned_at, status
            """

            condition_note = f"Returned in: {returned_condition}" if returned_condition else "Returned"

            update_result = await self.dal.execute(
                update_sql,
                [new_status, condition_note, checkout_id]
            )

            if not update_result:
                raise Exception("Failed to update checkout record")

            updated = update_result[0]

            logger.info(
                f"User {checkout['user_id']} returned {return_qty} of item "
                f"{checkout['item_id']} in community {community_id}"
            )

            return {
                'id': updated['id'],
                'item_id': updated['item_id'],
                'user_id': updated['user_id'],
                'quantity': updated['quantity'],
                'checked_out_at': updated['checked_out_at'],
                'due_at': updated['due_at'],
                'returned_at': updated['returned_at'],
                'status': updated['status']
            }

        except Exception as e:
            logger.error(f"Failed to checkin checkout {checkout_id}: {e}")
            raise

    # =========================================================================
    # STOCK MANAGEMENT
    # =========================================================================

    async def add_stock(
        self,
        community_id: int,
        item_id: int,
        quantity: int,
        reason: Optional[str] = None,
        added_by_user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Add stock to an inventory item.

        Args:
            community_id: Community ID
            item_id: Item ID
            quantity: Quantity to add
            reason: Reason for adding stock
            added_by_user_id: User ID who added the stock

        Returns:
            Item dictionary with updated quantities
        """
        try:
            if quantity <= 0:
                raise ValueError("Quantity to add must be positive")

            # Use stored procedure
            sql = """
                SELECT * FROM add_inventory_stock($1, $2, $3, $4, $5)
            """

            result = await self.dal.execute(
                sql,
                [item_id, quantity, added_by_user_id, community_id, reason]
            )

            if not result or not result[0]['success']:
                raise Exception(
                    result[0]['message'] if result else "Failed to add stock"
                )

            logger.info(
                f"Added {quantity} stock to item {item_id} in community {community_id}"
            )

            # Return updated item
            return await self.get_item(community_id, item_id)

        except Exception as e:
            logger.error(f"Failed to add stock to item {item_id}: {e}")
            raise

    async def remove_stock(
        self,
        community_id: int,
        item_id: int,
        quantity: int,
        reason: Optional[str] = None,
        removed_by_user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Remove stock from an inventory item.

        Args:
            community_id: Community ID
            item_id: Item ID
            quantity: Quantity to remove
            reason: Reason for removing stock
            removed_by_user_id: User ID who removed the stock

        Returns:
            Item dictionary with updated quantities
        """
        try:
            if quantity <= 0:
                raise ValueError("Quantity to remove must be positive")

            # Use stored procedure
            sql = """
                SELECT * FROM remove_inventory_stock($1, $2, $3, $4, $5)
            """

            result = await self.dal.execute(
                sql,
                [item_id, quantity, removed_by_user_id, community_id, reason]
            )

            if not result or not result[0]['success']:
                raise Exception(
                    result[0]['message'] if result else "Failed to remove stock"
                )

            logger.info(
                f"Removed {quantity} stock from item {item_id} in community {community_id}"
            )

            # Return updated item
            return await self.get_item(community_id, item_id)

        except Exception as e:
            logger.error(f"Failed to remove stock from item {item_id}: {e}")
            raise

    # =========================================================================
    # SEARCH AND FILTERING
    # =========================================================================

    async def search_items(
        self,
        community_id: int,
        query: str,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Full-text search for inventory items using PostgreSQL GIN index.

        Args:
            community_id: Community ID
            query: Search query
            limit: Maximum results to return

        Returns:
            List of matching items
        """
        try:
            # Use the database full-text search function
            sql = """
                SELECT * FROM search_inventory_items($1, $2, $3)
            """

            result = await self.dal.execute(
                sql,
                [community_id, query, limit]
            )

            logger.info(
                f"Full-text search in community {community_id}: "
                f"query='{query}', results={len(result)}"
            )

            return result if result else []

        except Exception as e:
            logger.error(f"Failed to search inventory items: {e}")
            raise

    async def get_items_by_category(
        self,
        community_id: int,
        category: str,
        include_unavailable: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Get inventory items by category.

        Args:
            community_id: Community ID
            category: Category name
            include_unavailable: Include items with 0 available quantity

        Returns:
            List of items in the category
        """
        try:
            availability_clause = ""
            if not include_unavailable:
                availability_clause = "AND available_quantity > 0"

            sql = f"""
                SELECT id, community_id, name, description, item_type, category,
                       quantity, available_quantity, checkout_price,
                       max_checkout_duration_hours, image_url, metadata,
                       created_at, updated_at
                FROM inventory_items
                WHERE community_id = $1 AND category = $2 AND deleted_at IS NULL
                {availability_clause}
                ORDER BY name
            """

            result = await self.dal.execute(
                sql,
                [community_id, category]
            )

            return result if result else []

        except Exception as e:
            logger.error(f"Failed to get items by category: {e}")
            raise

    async def get_items_by_type(
        self,
        community_id: int,
        item_type: str,
        include_unavailable: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Get inventory items by type.

        Args:
            community_id: Community ID
            item_type: Item type
            include_unavailable: Include items with 0 available quantity

        Returns:
            List of items of the specified type
        """
        try:
            availability_clause = ""
            if not include_unavailable:
                availability_clause = "AND available_quantity > 0"

            sql = f"""
                SELECT id, community_id, name, description, item_type, category,
                       quantity, available_quantity, checkout_price,
                       max_checkout_duration_hours, image_url, metadata,
                       created_at, updated_at
                FROM inventory_items
                WHERE community_id = $1 AND item_type = $2 AND deleted_at IS NULL
                {availability_clause}
                ORDER BY name
            """

            result = await self.dal.execute(
                sql,
                [community_id, item_type]
            )

            return result if result else []

        except Exception as e:
            logger.error(f"Failed to get items by type: {e}")
            raise

    async def get_available_items(
        self,
        community_id: int,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get all available inventory items in a community.

        Args:
            community_id: Community ID
            limit: Maximum items to return

        Returns:
            List of available items
        """
        try:
            limit_clause = f"LIMIT {limit}" if limit else ""

            sql = f"""
                SELECT id, community_id, name, description, item_type, category,
                       quantity, available_quantity, checkout_price,
                       max_checkout_duration_hours, image_url, metadata,
                       created_at, updated_at
                FROM inventory_items
                WHERE community_id = $1 AND available_quantity > 0 AND deleted_at IS NULL
                ORDER BY available_quantity DESC
                {limit_clause}
            """

            result = await self.dal.execute(
                sql,
                [community_id]
            )

            return result if result else []

        except Exception as e:
            logger.error(f"Failed to get available items: {e}")
            raise

    async def get_low_stock_items(
        self,
        community_id: int
    ) -> List[Dict[str, Any]]:
        """
        Get items with low stock (available_quantity < total quantity).

        Args:
            community_id: Community ID

        Returns:
            List of low stock items
        """
        try:
            sql = """
                SELECT id, community_id, name, description, item_type, category,
                       quantity, available_quantity, checkout_price,
                       max_checkout_duration_hours, image_url, metadata,
                       created_at, updated_at
                FROM inventory_items
                WHERE community_id = $1 AND available_quantity < quantity
                      AND deleted_at IS NULL
                ORDER BY available_quantity ASC
            """

            result = await self.dal.execute(sql, [community_id])
            return result if result else []

        except Exception as e:
            logger.error(f"Failed to get low stock items: {e}")
            raise

    # =========================================================================
    # CHECKOUT TRACKING
    # =========================================================================

    async def get_active_checkouts(
        self,
        community_id: int,
        user_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get active checkouts.

        Args:
            community_id: Community ID
            user_id: Optional - filter by specific user

        Returns:
            List of active checkout records
        """
        try:
            if user_id:
                sql = """
                    SELECT id, item_id, user_id, community_id, quantity,
                           checked_out_at, due_at, returned_at, status, notes
                    FROM inventory_checkouts
                    WHERE community_id = $1 AND user_id = $2 AND status = 'active'
                    ORDER BY due_at ASC NULLS LAST
                """
                result = await self.dal.execute(sql, [community_id, user_id])
            else:
                sql = """
                    SELECT id, item_id, user_id, community_id, quantity,
                           checked_out_at, due_at, returned_at, status, notes
                    FROM inventory_checkouts
                    WHERE community_id = $1 AND status = 'active'
                    ORDER BY due_at ASC NULLS LAST
                """
                result = await self.dal.execute(sql, [community_id])

            return result if result else []

        except Exception as e:
            logger.error(f"Failed to get active checkouts: {e}")
            raise

    async def get_overdue_checkouts(
        self,
        community_id: int
    ) -> List[Dict[str, Any]]:
        """
        Get overdue checkouts (due_at passed).

        Args:
            community_id: Community ID

        Returns:
            List of overdue checkout records
        """
        try:
            sql = """
                SELECT id, item_id, user_id, community_id, quantity,
                       checked_out_at, due_at, returned_at, status, notes
                FROM inventory_checkouts
                WHERE community_id = $1 AND status = 'active' AND due_at < NOW()
                ORDER BY due_at ASC
            """

            result = await self.dal.execute(sql, [community_id])
            return result if result else []

        except Exception as e:
            logger.error(f"Failed to get overdue checkouts: {e}")
            raise

    async def get_user_checkouts(
        self,
        community_id: int,
        user_id: int,
        status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get checkout history for a specific user.

        Args:
            community_id: Community ID
            user_id: User ID
            status: Optional filter by status (active, returned, overdue)

        Returns:
            List of checkout records
        """
        try:
            if status:
                sql = """
                    SELECT id, item_id, user_id, community_id, quantity,
                           checked_out_at, due_at, returned_at, status, notes
                    FROM inventory_checkouts
                    WHERE community_id = $1 AND user_id = $2 AND status = $3
                    ORDER BY checked_out_at DESC
                """
                result = await self.dal.execute(sql, [community_id, user_id, status])
            else:
                sql = """
                    SELECT id, item_id, user_id, community_id, quantity,
                           checked_out_at, due_at, returned_at, status, notes
                    FROM inventory_checkouts
                    WHERE community_id = $1 AND user_id = $2
                    ORDER BY checked_out_at DESC
                """
                result = await self.dal.execute(sql, [community_id, user_id])

            return result if result else []

        except Exception as e:
            logger.error(f"Failed to get user checkouts: {e}")
            raise

    # =========================================================================
    # INVENTORY SUMMARY AND STATISTICS
    # =========================================================================

    async def get_inventory_summary(self, community_id: int) -> Dict[str, Any]:
        """
        Get comprehensive inventory summary for a community.

        Args:
            community_id: Community ID

        Returns:
            Dictionary with inventory statistics
        """
        try:
            sql = """
                SELECT * FROM get_inventory_summary($1)
            """

            result = await self.dal.execute(sql, [community_id])

            if result:
                summary = result[0]
                return {
                    'total_items': summary['total_items'],
                    'total_quantity': summary['total_quantity'],
                    'total_available': summary['total_available'],
                    'active_checkouts': summary['active_checkouts'],
                    'overdue_checkouts': summary['overdue_checkouts'],
                    'low_stock_items': summary['low_stock_items']
                }

            return {
                'total_items': 0,
                'total_quantity': 0,
                'total_available': 0,
                'active_checkouts': 0,
                'overdue_checkouts': 0,
                'low_stock_items': 0
            }

        except Exception as e:
            logger.error(f"Failed to get inventory summary: {e}")
            raise

    # =========================================================================
    # AUDIT LOGGING
    # =========================================================================

    async def get_audit_log(
        self,
        community_id: int,
        item_id: Optional[int] = None,
        user_id: Optional[int] = None,
        action: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get audit log entries for inventory operations.

        Args:
            community_id: Community ID
            item_id: Optional filter by item
            user_id: Optional filter by user
            action: Optional filter by action type
            limit: Maximum log entries to return

        Returns:
            List of audit log entries
        """
        try:
            where_clauses = ["community_id = $1"]
            params = [community_id]
            param_count = 2

            if item_id:
                where_clauses.append(f"item_id = ${param_count}")
                params.append(item_id)
                param_count += 1

            if user_id:
                where_clauses.append(f"user_id = ${param_count}")
                params.append(user_id)
                param_count += 1

            if action:
                where_clauses.append(f"action = ${param_count}")
                params.append(action)
                param_count += 1

            where_clause = " AND ".join(where_clauses)

            sql = f"""
                SELECT id, item_id, user_id, community_id, action,
                       quantity_change, details, created_at
                FROM inventory_log
                WHERE {where_clause}
                ORDER BY created_at DESC
                LIMIT ${{param_count}}
            """

            sql = sql.replace(f'${{{param_count}}}', f'${param_count}')
            params.append(limit)

            result = await self.dal.execute(sql, params)
            return result if result else []

        except Exception as e:
            logger.error(f"Failed to get audit log: {e}")
            raise

    async def _log_action(
        self,
        action: str,
        community_id: int,
        item_id: Optional[int] = None,
        user_id: Optional[int] = None,
        quantity_change: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Log an inventory action to the audit trail.

        Args:
            action: Action type
            community_id: Community ID
            item_id: Optional item ID
            user_id: Optional user ID
            quantity_change: Optional quantity change
            details: Optional additional details
        """
        try:
            sql = """
                INSERT INTO inventory_log
                (item_id, user_id, community_id, action, quantity_change, details)
                VALUES ($1, $2, $3, $4, $5, $6)
            """

            await self.dal.execute(
                sql,
                [
                    item_id,
                    user_id,
                    community_id,
                    action,
                    quantity_change,
                    details
                ]
            )

        except Exception as e:
            logger.error(f"Failed to log action: {e}")
            # Don't raise - logging failure shouldn't break operations
