import { apiClient } from '../lib/apiClient';

// Mirrors `superadminTenantApi` in `services/api.js` (same endpoints) with
// request/response shapes copied from the source of truth --
// `hub_api/blueprints/v1/superadmin.py` `TenantDTO`/`TenantsResponse`/
// `CreateTenantRequest`/`UpdateTenantRequest` -- NOT from the legacy JS
// helper, which calls methods (`getTenants`/`createTenant`/`updateTenant`/
// `deactivateTenant`) that don't exist on its own export
// (`list`/`create`/`update`/`delete`). Per `src/services/README.md`, this
// file builds on `src/lib/apiClient.ts`; `services/api.js` is untouched.

/**
 * A tenant is one private customer organization. `isGlobal` marks tenant 0,
 * the shared catch-all tenant for individual/free customers -- it is not a
 * normal customer org and the UI must label it distinctly (never offer
 * deactivation for it).
 */
export interface TenantDTO {
  id: number;
  slug: string;
  displayName: string;
  description: string | null;
  logoUrl: string | null;
  isGlobal: boolean;
  isActive: boolean;
  allowedModuleIds: number[] | null;
  seatLimit: number | null;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface PaginationMeta {
  page: number;
  limit: number;
  total: number;
  totalPages: number;
}

export interface ListTenantsResponse {
  success: boolean;
  tenants: TenantDTO[];
  pagination: PaginationMeta;
}

export interface ListTenantsParams {
  page: number;
  limit: number;
  search?: string;
}

export interface CreateTenantPayload {
  slug: string;
  displayName: string;
  description?: string | null;
  logoUrl?: string | null;
  seatLimit?: number | null;
  allowedModuleIds?: number[] | null;
}

export interface CreatedTenantDTO {
  id: number;
  slug: string;
  displayName: string;
  createdAt: string | null;
}

export interface CreateTenantResponse {
  success: boolean;
  tenant: CreatedTenantDTO;
}

export interface UpdateTenantPayload {
  displayName?: string;
  description?: string | null;
  logoUrl?: string | null;
  isActive?: boolean;
  seatLimit?: number | null;
  allowedModuleIds?: number[] | null;
  config?: Record<string, unknown> | null;
}

export interface MessageResponse {
  success: boolean;
  message: string;
}

export const superTenantApi = {
  list: (params: ListTenantsParams) =>
    apiClient.get<ListTenantsResponse>('/api/v1/superadmin/tenants', { params }),
  create: (data: CreateTenantPayload) =>
    apiClient.post<CreateTenantResponse>('/api/v1/superadmin/tenants', data),
  update: (id: number, data: UpdateTenantPayload) =>
    apiClient.put<MessageResponse>(`/api/v1/superadmin/tenants/${id}`, data),
  deactivate: (id: number) =>
    apiClient.delete<MessageResponse>(`/api/v1/superadmin/tenants/${id}`),
};
