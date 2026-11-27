import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import { publicApi } from '../../services/api';
import ResendVerification from './ResendVerification';

function RegisterPage() {
  const navigate = useNavigate();
  const { register, isAuthenticated } = useAuth();
  const [settings, setSettings] = useState({ loading: true, enabled: false });
  const [form, setForm] = useState({
    username: '',
    email: '',
    password: '',
    confirmPassword: '',
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  // Account created but pending email verification (or the mail could not be sent yet).
  const [pending, setPending] = useState(null);

  useEffect(() => {
    publicApi.getSignupSettings()
      .then(({ data }) => {
        setSettings({
          loading: false,
          enabled: data.signupEnabled === true,
          allowedDomains: data.allowedDomains,
        });
      })
      .catch(() => setSettings({ loading: false, enabled: false }));
  }, []);

  useEffect(() => {
    if (isAuthenticated) navigate('/dashboard', { replace: true });
  }, [isAuthenticated, navigate]);

  const updateField = (event) => {
    setForm(current => ({ ...current, [event.target.name]: event.target.value }));
  };

  const handleSubmit = async (event) => {
    event.preventDefault();
    setError('');

    if (form.password !== form.confirmPassword) {
      setError('Passwords do not match.');
      return;
    }

    setSubmitting(true);
    try {
      const result = await register(form.email, form.password, form.username);
      if (result?.requiresVerification) {
        setMessage(result.message || 'Check your email to verify your account.');
        setPending({ email: form.email, error: '' });
      } else {
        navigate('/dashboard');
      }
    } catch (err) {
      if (err.code === 'verification_email_failed') {
        // Account exists but the mail did not go out: never a dead end, offer a resend.
        setPending({ email: form.email, error: err.message });
        return;
      }
      setError(err.message || 'Registration failed.');
    } finally {
      setSubmitting(false);
    }
  };

  if (settings.loading) {
    return <div className="min-h-[60vh] flex items-center justify-center text-navy-300">Loading registration…</div>;
  }

  if (!settings.enabled) {
    return (
      <div className="min-h-[60vh] flex items-center justify-center px-4">
        <div className="card max-w-md w-full p-6 text-center">
          <h1 className="text-2xl font-bold text-gold-400">Registration unavailable</h1>
          <p className="mt-3 text-navy-300">
            New account registration is not enabled on this WaddleBot instance.
          </p>
          <Link to="/login" className="inline-block mt-6 text-sky-400 hover:text-sky-300">
            Return to sign in
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-[80vh] flex items-center justify-center py-12 px-4">
      <div className="card max-w-md w-full p-6">
        <h1 className="text-3xl font-bold text-gold-400">Create your account</h1>
        <p className="mt-2 text-navy-300">Join WaddleBot with an email and password.</p>

        {settings.allowedDomains?.length > 0 && (
          <p className="mt-2 text-xs text-navy-400">
            Registration is limited to: {settings.allowedDomains.join(', ')}
          </p>
        )}

        {error && (
          <div className="mt-4 p-3 rounded-lg bg-red-500/20 border border-red-500/30 text-red-300" role="alert">
            {error}
          </div>
        )}
        {message && (
          <div className="mt-4 p-3 rounded-lg bg-green-500/20 border border-green-500/30 text-green-300" role="status">
            {message}
          </div>
        )}

        {pending && (
          <ResendVerification initialEmail={pending.email} initialError={pending.error} />
        )}

        {!message && !pending && (
          <form onSubmit={handleSubmit} className="mt-6 space-y-4">
            <label className="block text-sky-200">
              Username
              <input
                className="input mt-1 w-full"
                name="username"
                value={form.username}
                onChange={updateField}
                minLength={3}
                maxLength={50}
                pattern="[A-Za-z0-9_-]+"
                autoComplete="username"
                required
                data-testid="username-input"
              />
            </label>
            <label className="block text-sky-200">
              Email
              <input
                className="input mt-1 w-full"
                type="email"
                name="email"
                value={form.email}
                onChange={updateField}
                autoComplete="email"
                required
                data-testid="email-input"
              />
            </label>
            <label className="block text-sky-200">
              Password
              <input
                className="input mt-1 w-full"
                type="password"
                name="password"
                value={form.password}
                onChange={updateField}
                minLength={8}
                autoComplete="new-password"
                required
                data-testid="password-input"
              />
              <span className="block mt-1 text-xs text-navy-400">
                At least 8 characters with uppercase, lowercase, and a number.
              </span>
            </label>
            <label className="block text-sky-200">
              Confirm password
              <input
                className="input mt-1 w-full"
                type="password"
                name="confirmPassword"
                value={form.confirmPassword}
                onChange={updateField}
                minLength={8}
                autoComplete="new-password"
                required
              />
            </label>
            <button
              type="submit"
              disabled={submitting}
              className="btn btn-primary w-full disabled:opacity-50"
              data-testid="auth-submit"
            >
              {submitting ? 'Creating account…' : 'Create Account'}
            </button>
          </form>
        )}

        <p className="mt-6 text-center text-navy-400">
          Already have an account?{' '}
          <Link to="/login" className="text-sky-400 hover:text-sky-300">Sign in</Link>
        </p>
      </div>
    </div>
  );
}

export default RegisterPage;
