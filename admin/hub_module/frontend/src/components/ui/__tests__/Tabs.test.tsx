import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { Tabs, type TabItem } from '../Tabs';
import { axeViolations } from '../../../test/axe';

const TABS: TabItem[] = [
  { id: 'settings', label: 'Settings' },
  { id: 'creators', label: 'Creators', badge: 3 },
  { id: 'history', label: 'History', disabled: true },
  { id: 'audit', label: 'Audit' },
];

function Harness() {
  const [value, setValue] = useState('settings');
  return (
    <Tabs tabs={TABS} value={value} onChange={setValue} label="Shoutout sections">
      <p>Panel for {value}</p>
    </Tabs>
  );
}

describe('Tabs', () => {
  it('exposes tablist/tab/tabpanel roles with correct wiring', () => {
    render(<Harness />);
    expect(screen.getByRole('tablist', { name: 'Shoutout sections' })).toBeInTheDocument();
    const selected = screen.getByRole('tab', { name: 'Settings' });
    expect(selected).toHaveAttribute('aria-selected', 'true');
    expect(selected).toHaveAttribute('tabindex', '0');
    expect(screen.getByRole('tab', { name: /Creators/ })).toHaveAttribute('tabindex', '-1');
    const panel = screen.getByRole('tabpanel');
    expect(panel).toHaveAccessibleName('Settings');
    expect(selected.getAttribute('aria-controls')).toBe(panel.id);
  });

  it('activates a tab on click', () => {
    render(<Harness />);
    fireEvent.click(screen.getByRole('tab', { name: /Creators/ }));
    expect(screen.getByText('Panel for creators')).toBeInTheDocument();
  });

  it('moves with arrow keys, skips disabled tabs, wraps, and supports Home/End', () => {
    render(<Harness />);
    const settings = screen.getByRole('tab', { name: 'Settings' });
    fireEvent.keyDown(settings, { key: 'ArrowRight' });
    expect(screen.getByRole('tab', { name: /Creators/ })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('tab', { name: /Creators/ }), { key: 'ArrowRight' });
    expect(screen.getByRole('tab', { name: 'Audit' })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Audit' }), { key: 'ArrowRight' });
    expect(screen.getByRole('tab', { name: 'Settings' })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Settings' }), { key: 'ArrowLeft' });
    expect(screen.getByRole('tab', { name: 'Audit' })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Audit' }), { key: 'Home' });
    expect(screen.getByRole('tab', { name: 'Settings' })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Settings' }), { key: 'End' });
    expect(screen.getByRole('tab', { name: 'Audit' })).toHaveFocus();
  });

  it('ignores unrelated keys', () => {
    render(<Harness />);
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Settings' }), { key: 'a' });
    expect(screen.getByRole('tab', { name: 'Settings' })).toHaveAttribute('aria-selected', 'true');
  });

  it('has no axe violations', async () => {
    const { container } = render(<Harness />);
    expect(await axeViolations(container)).toEqual([]);
  });
});
