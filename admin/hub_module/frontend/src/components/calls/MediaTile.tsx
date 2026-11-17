import { useEffect, useRef, useState } from 'react';
import { MicrophoneIcon } from '@heroicons/react/24/solid';

export interface MediaTileProps {
  /** Display name, never an id. */
  name: string;
  stream: MediaStream | null;
  /** Local preview: never played back to the user (avoids feedback). */
  local?: boolean;
  /** Local mute state, shown as a badge on the local tile. */
  muted?: boolean;
  /** "Connecting" overlay for a remote stream that has not produced media yet. */
  connecting?: boolean;
}

/** Re-evaluates whether a stream currently has a live video track as tracks arrive or end. */
function useHasVideo(stream: MediaStream | null): boolean {
  const read = (): boolean => stream?.getVideoTracks().some((track) => track.readyState !== 'ended') ?? false;
  const [hasVideo, setHasVideo] = useState(read);
  useEffect(() => {
    setHasVideo(read());
    if (stream === null) return undefined;
    const update = (): void => setHasVideo(read());
    stream.addEventListener('addtrack', update);
    stream.addEventListener('removetrack', update);
    return () => {
      stream.removeEventListener('addtrack', update);
      stream.removeEventListener('removetrack', update);
    };
    // `read` closes over `stream` only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stream]);
  return hasVideo;
}

/** One participant tile: live video when there is any, otherwise an avatar initial; audio plays through the element. */
export function MediaTile({ name, stream, local = false, muted = false, connecting = false }: MediaTileProps) {
  const ref = useRef<HTMLVideoElement>(null);
  const hasVideo = useHasVideo(stream);

  useEffect(() => {
    const element = ref.current;
    if (element === null) return undefined;
    element.srcObject = stream;
    return () => {
      element.srcObject = null;
    };
  }, [stream]);

  return (
    <figure className="relative overflow-hidden rounded-control bg-surface-raised" data-testid="media-tile" aria-label={name}>
      <video
        ref={ref}
        autoPlay
        playsInline
        muted={local}
        className={hasVideo ? 'aspect-video w-full object-cover' : 'sr-only'}
        data-testid={local ? 'local-video' : 'remote-video'}
      />
      {hasVideo ? null : (
        <div aria-hidden="true" className="flex aspect-video w-full items-center justify-center bg-surface text-3xl font-semibold text-fg-secondary">
          {name.charAt(0).toUpperCase()}
        </div>
      )}
      <figcaption className="flex items-center justify-between gap-2 px-3 py-2 text-sm text-fg">
        <span className="truncate">{local ? `${name} (you)` : name}</span>
        {local && muted ? (
          <span className="flex items-center gap-1 text-xs text-danger-fg">
            <MicrophoneIcon aria-hidden="true" className="h-3.5 w-3.5" />
            Muted
          </span>
        ) : null}
        {connecting ? <span className="text-xs text-fg-muted">Connecting</span> : null}
      </figcaption>
    </figure>
  );
}
