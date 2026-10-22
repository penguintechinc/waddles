import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { CheckboxField, SelectField, SwitchField, TextareaField, TextField } from '../Field';
import { axeViolations } from '../../../test/axe';

describe('TextField', () => {
  it('associates the label with the input', () => {
    render(<TextField label="Display name" />);
    expect(screen.getByLabelText('Display name')).toBeInstanceOf(HTMLInputElement);
  });

  it('marks required fields and hides the asterisk from assistive tech', () => {
    render(<TextField label="Email" required />);
    const input = screen.getByLabelText(/Email/);
    expect(input).toBeRequired();
    expect(screen.getByText('*')).toHaveAttribute('aria-hidden', 'true');
  });

  it('keeps the label for screen readers when visually hidden', () => {
    render(<TextField label="Search members" hideLabel />);
    const label = screen.getByText('Search members');
    expect(label).toHaveClass('sr-only');
    expect(screen.getByLabelText('Search members')).toBeInTheDocument();
  });

  it('wires hint and error text through aria-describedby and flags invalid state', () => {
    render(<TextField label="Handle" hint="Letters only" error="Handle is taken" />);
    const input = screen.getByLabelText('Handle');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    const describedBy = input.getAttribute('aria-describedby') ?? '';
    expect(describedBy.split(' ')).toHaveLength(2);
    expect(document.getElementById(describedBy.split(' ')[0] ?? '')).toHaveTextContent('Letters only');
    expect(document.getElementById(describedBy.split(' ')[1] ?? '')).toHaveTextContent('Handle is taken');
  });

  it('omits aria-invalid and aria-describedby when there is nothing to describe', () => {
    render(<TextField label="Plain" />);
    const input = screen.getByLabelText('Plain');
    expect(input).not.toHaveAttribute('aria-invalid');
    expect(input).not.toHaveAttribute('aria-describedby');
  });

  it('honours a caller-supplied id and forwards input props', () => {
    const onChange = vi.fn();
    render(<TextField label="Port" id="port" type="number" onChange={onChange} />);
    const input = screen.getByLabelText('Port');
    expect(input).toHaveAttribute('id', 'port');
    expect(input).toHaveAttribute('type', 'number');
    fireEvent.change(input, { target: { value: '8080' } });
    expect(onChange).toHaveBeenCalled();
  });
});

describe('SelectField / TextareaField', () => {
  it('labels a select and its options', () => {
    render(
      <SelectField label="Platform" defaultValue="twitch">
        <option value="discord">Discord</option>
        <option value="twitch">Twitch</option>
      </SelectField>,
    );
    expect(screen.getByLabelText('Platform')).toHaveValue('twitch');
  });

  it('labels a textarea and reports its error', () => {
    render(<TextareaField label="Bio" error="Too long" />);
    expect(screen.getByLabelText('Bio')).toHaveAttribute('aria-invalid', 'true');
  });
});

describe('CheckboxField / SwitchField', () => {
  it('toggles a checkbox by clicking its label', () => {
    const onChange = vi.fn();
    render(<CheckboxField label="Send digest" hint="Weekly" onChange={onChange} />);
    fireEvent.click(screen.getByText('Send digest'));
    expect(onChange).toHaveBeenCalled();
    expect(screen.getByLabelText('Send digest')).toBeChecked();
    expect(screen.getByLabelText('Send digest')).toHaveAccessibleDescription('Weekly');
  });

  it('exposes the switch role with an accessible name and description', () => {
    render(<SwitchField label="Event sync" description="Mirror events to Discord" />);
    const toggle = screen.getByRole('switch', { name: 'Event sync' });
    expect(toggle).toHaveAccessibleDescription('Mirror events to Discord');
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    expect(toggle).toBeChecked();
  });

  it('exposes a disabled switch as disabled to assistive tech', () => {
    render(<SwitchField label="Locked" disabled />);
    expect(screen.getByRole('switch', { name: 'Locked' })).toBeDisabled();
  });
});

describe('form fixture', () => {
  it('has no axe violations', async () => {
    const { container } = render(
      <form aria-label="Profile">
        <TextField label="Name" hint="Public" />
        <TextField label="Email" error="Invalid" required />
        <SelectField label="Region">
          <option>EU</option>
        </SelectField>
        <TextareaField label="Bio" />
        <CheckboxField label="Newsletter" />
        <SwitchField label="Notifications" />
      </form>,
    );
    expect(await axeViolations(container)).toEqual([]);
  });
});
