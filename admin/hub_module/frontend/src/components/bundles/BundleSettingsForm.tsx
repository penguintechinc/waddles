import Form from '@rjsf/core';
import type { RJSFSchema } from '@rjsf/utils';
import validator from '@rjsf/validator-ajv8';
import type { ReactNode } from 'react';
import type { ConfigFieldError } from '../../services/bundleAdminApi';
import { toErrorSchema } from './settingsHelpers';

interface Props {
  schema: Record<string, unknown>;
  value: Record<string, unknown>;
  readOnly: boolean;
  serverErrors: ConfigFieldError[];
  onChange: (value: Record<string, unknown>) => void;
  onSubmit: (value: Record<string, unknown>) => void;
  /** Action buttons rendered inside the form (Save is `type="submit"`). */
  children?: ReactNode;
}

/**
 * Generic JSON-Schema (Draft 2020-12) form for one bundle's community config,
 * rendered by @rjsf/core with the ajv8 validator. Client validation is a UX
 * nicety only -- the PUT is re-validated by hub-api, whose errors are shown inline.
 */
export default function BundleSettingsForm({
  schema,
  value,
  readOnly,
  serverErrors,
  onChange,
  onSubmit,
  children,
}: Props) {
  return (
    <Form
      className="bundle-settings-form"
      schema={schema as RJSFSchema}
      formData={value}
      validator={validator}
      readonly={readOnly}
      showErrorList={false}
      extraErrors={toErrorSchema(serverErrors)}
      onChange={(e) => onChange((e.formData ?? {}) as Record<string, unknown>)}
      onSubmit={(e) => onSubmit((e.formData ?? {}) as Record<string, unknown>)}
    >
      {children ?? <span />}
    </Form>
  );
}
