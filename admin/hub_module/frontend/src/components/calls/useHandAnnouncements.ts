import { useEffect, useRef, useState } from 'react';
import type { RaisedHand } from '../../services/callsApi';

/**
 * Builds a polite screen-reader sentence whenever the hands queue changes (someone raised or lowered a
 * hand). The first snapshot is the baseline and announces nothing, so loading a page is not noisy.
 */
export function useHandAnnouncements(hands: readonly RaisedHand[] | undefined): string {
  const previous = useRef<Map<string, string> | null>(null);
  const [message, setMessage] = useState('');

  useEffect(() => {
    if (hands === undefined) return;
    const current = new Map(hands.map((hand) => [hand.participantId, hand.displayName]));
    const before = previous.current;
    previous.current = current;
    if (before === null) return;
    const raised = [...current].filter(([id]) => !before.has(id)).map(([, name]) => name);
    const lowered = [...before].filter(([id]) => !current.has(id)).map(([, name]) => name);
    const parts: string[] = [];
    if (raised.length > 0) parts.push(`${raised.join(', ')} raised a hand.`);
    if (lowered.length > 0) parts.push(`${lowered.join(', ')} lowered a hand.`);
    if (parts.length > 0) parts.push(`${current.size} ${current.size === 1 ? 'hand' : 'hands'} raised.`);
    setMessage(parts.join(' '));
  }, [hands]);

  return message;
}
