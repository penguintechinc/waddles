// GENERATED FILE -- do not edit by hand.
// Source: wit/waddle-bundle/stage.wit, regenerated via
// scripts/generate-guest-types.sh (jco guest-types).

declare module 'waddle:bundle/db@1.0.0' {
  /**
   * Statement text with $1..$n placeholders. String interpolation of
   * parameters is impossible across this boundary by construction.
   */
  export function execute(statement: string, params: Array<Value>): Rows;
  export type Value = ValueNullValue | ValueBoolValue | ValueIntValue | ValueFloatValue | ValueTextValue | ValueBytesValue;
  export interface ValueNullValue {
    tag: 'null-value',
  }
  export interface ValueBoolValue {
    tag: 'bool-value',
    val: boolean,
  }
  export interface ValueIntValue {
    tag: 'int-value',
    val: bigint,
  }
  export interface ValueFloatValue {
    tag: 'float-value',
    val: number,
  }
  export interface ValueTextValue {
    tag: 'text-value',
    val: string,
  }
  export interface ValueBytesValue {
    tag: 'bytes-value',
    val: Uint8Array,
  }
  export interface Rows {
    columns: Array<string>,
    rows: Array<Array<Value>>,
    rowsAffected: bigint,
  }
  export type Error = ErrorDenied | ErrorSyntax | ErrorConflict | ErrorTimeout | ErrorBackend;
  export interface ErrorDenied {
    tag: 'denied',
    val: string,
  }
  export interface ErrorSyntax {
    tag: 'syntax',
    val: string,
  }
  export interface ErrorConflict {
    tag: 'conflict',
    val: string,
  }
  export interface ErrorTimeout {
    tag: 'timeout',
  }
  export interface ErrorBackend {
    tag: 'backend',
    val: string,
  }
}
