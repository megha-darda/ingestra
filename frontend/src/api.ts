export type Kind = 'electronics' | 'groceries'
export type Counts = Record<string, number>
export type Product = {sku:string; name:string; quantity:number; price:string; inventoryType:Kind; version:number; revision:string; category?:string; warrantyMonths?:number; expiryDate?:string}
export type Result = {sku?:string; row?:number; status:string; message?:string; resource?:string|number}
export type Journal = {resourceKey:string; operation:string; beforeVersion:number|null; afterVersion:number; rollbackStatus:string; beforeImage:Product|null; afterImage:Product; operationId:string}
export type Job = {jobId:string; inventoryType:Kind; filename:string; status:string; createdAt:string; updatedAt:string; planId?:string; previewCounts?:Counts; executionCounts?:Counts; rollbackCounts?:Counts; errorMessage?:string; executionResults?:Result[]}
export type PlanRow = {index:number; row:number; operation:string; data:Partial<Product>; errors:string[]; beforeImage:Product|null; expected:{version:number; revision:string}|null}
export type Recovery = {counts:Counts; items:Result[]; journal:Journal[]}
export const BASE = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:3001').replace(/\/$/, '')
export async function api<T>(path:string, method='GET', body?:unknown):Promise<T> {
  let response:Response
  try {
    response = await fetch(BASE + path, {method, headers:body ? {'Content-Type':'application/json'} : {}, body:body ? JSON.stringify(body) : undefined})
  } catch {
    throw new Error('Unable to reach Ingestra. Check the API connection and try again.')
  }
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    const fallback = response.status === 429 ? 'The demo is receiving too many requests. Please try again shortly.'
      : response.status === 413 ? 'This file exceeds the 2 MiB upload limit.'
      : `Request failed (${response.status}). Please try again.`
    throw new Error(response.status === 429 ? fallback : data?.error?.message || data?.message || fallback)
  }
  if (data === null) throw new Error('The API returned an unexpected response. Please try again.')
  return data as T
}
export const running = (job?:Job) => !!job && ['PREVIEWING','IMPORTING','ROLLING_BACK'].includes(job.status)
export const recoverable = (job:Job) => ['COMPLETED','PARTIAL','FAILED','ROLLED_BACK','ROLLBACK_PARTIAL','ROLLBACK_FAILED'].includes(job.status)
