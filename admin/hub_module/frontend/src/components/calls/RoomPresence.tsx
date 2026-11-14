export interface RoomPresenceProps {
  total: number | undefined;
  speaking: number | undefined;
}

/** Member view of who is in the room: head counts only (names are exposed to moderators). */
export function RoomPresence({ total, speaking }: RoomPresenceProps) {
  const people = total ?? 0;
  const speakers = speaking ?? 0;
  return (
    <section aria-labelledby="presence-title" data-testid="room-presence">
      <h3 id="presence-title" className="mb-2 text-sm font-medium text-fg-secondary">
        In this room
      </h3>
      <p className="text-sm text-fg">
        {people} {people === 1 ? 'person' : 'people'}, {speakers} speaking
      </p>
    </section>
  );
}
