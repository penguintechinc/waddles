import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { Button } from '../Button';
import { IconButton } from '../IconButton';
import { axeViolations } from '../../../test/axe';

describe('Button', () => {
  it('defaults to type="button" so it never submits a surrounding form', () => {
    render(<Button>Save</Button>);
    expect(screen.getByRole('button', { name: 'Save' })).toHaveAttribute('type', 'button');
  });

  it('applies the variant and size classes', () => {
    render(
      <Button variant="primary" size="sm">
        Go
      </Button>,
    );
    const btn = screen.getByRole('button', { name: 'Go' });
    expect(btn).toHaveClass('btn', 'btn-primary', 'btn-sm');
  });

  it('fires onClick when enabled', () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Run</Button>);
    fireEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('blocks activation and announces busy state while loading', () => {
    const onClick = vi.fn();
    render(
      <Button loading onClick={onClick}>
        Saving
      </Button>,
    );
    const btn = screen.getByRole('button', { name: /saving/i });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('status')).toHaveTextContent('Working');
    fireEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('hides a decorative leading icon from assistive tech', () => {
    render(<Button leadingIcon={<svg data-testid="icon" />}>Add</Button>);
    expect(screen.getByTestId('icon').parentElement).toHaveAttribute('aria-hidden', 'true');
  });

  it('has no axe violations', async () => {
    const { container } = render(
      <div>
        <Button variant="primary">Primary</Button>
        <Button variant="danger" disabled>
          Delete
        </Button>
        <IconButton label="Close" icon={<svg />} />
      </div>,
    );
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe('IconButton', () => {
  it('exposes its label as the accessible name and tooltip', () => {
    render(<IconButton label="Delete channel" icon={<svg />} tone="danger" />);
    const btn = screen.getByRole('button', { name: 'Delete channel' });
    expect(btn).toHaveAttribute('title', 'Delete channel');
    expect(btn).toHaveClass('h-10', 'w-10');
  });
});
