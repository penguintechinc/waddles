import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { onActivate } from '../a11y';

describe('onActivate', () => {
  it('runs the action on Enter and Space and prevents default scrolling', () => {
    const action = vi.fn();
    render(
      <div role="button" tabIndex={0} onKeyDown={onActivate(action)}>
        Open
      </div>,
    );
    const el = screen.getByRole('button', { name: 'Open' });
    fireEvent.keyDown(el, { key: 'Enter' });
    fireEvent.keyDown(el, { key: ' ' });
    expect(action).toHaveBeenCalledTimes(2);
  });

  it('ignores other keys', () => {
    const action = vi.fn();
    render(
      <div role="button" tabIndex={0} onKeyDown={onActivate(action)}>
        Open
      </div>,
    );
    fireEvent.keyDown(screen.getByRole('button'), { key: 'a' });
    fireEvent.keyDown(screen.getByRole('button'), { key: 'Escape' });
    expect(action).not.toHaveBeenCalled();
  });

  it('does not hijack keys that bubble from a nested control', () => {
    const action = vi.fn();
    render(
      <div role="button" tabIndex={0} onKeyDown={onActivate(action)}>
        <input aria-label="inner" />
      </div>,
    );
    fireEvent.keyDown(screen.getByLabelText('inner'), { key: 'Enter' });
    expect(action).not.toHaveBeenCalled();
  });
});
