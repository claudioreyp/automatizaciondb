import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "npm:@supabase/supabase-js@2.57.4";

const jsonHeaders = { "Content-Type": "application/json" };

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: jsonHeaders });
}

function isStrongPassword(password: string): boolean {
  return password.length >= 12
    && /[a-z]/.test(password)
    && /[A-Z]/.test(password)
    && /\d/.test(password)
    && /[^A-Za-z0-9]/.test(password);
}

Deno.serve(async (request) => {
  if (request.method !== "POST") {
    return jsonResponse({ detail: "Method not allowed" }, 405);
  }

  const authorization = request.headers.get("Authorization");
  if (!authorization?.toLowerCase().startsWith("bearer ")) {
    return jsonResponse({ detail: "Unauthorized" }, 401);
  }

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !serviceRoleKey) {
    return jsonResponse({ detail: "Auth service unavailable" }, 503);
  }

  const admin = createClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const accessToken = authorization.slice("Bearer ".length).trim();
  const { data: callerData, error: callerError } = await admin.auth.getUser(accessToken);
  const caller = callerData.user;
  if (callerError || !caller) {
    return jsonResponse({ detail: "Unauthorized" }, 401);
  }
  if (caller.app_metadata?.role !== "superadmin"
    || caller.app_metadata?.account_scope !== "escalar_ai_admin") {
    return jsonResponse({ detail: "Forbidden" }, 403);
  }

  let payload: Record<string, unknown>;
  try {
    payload = await request.json();
  } catch {
    return jsonResponse({ detail: "Invalid JSON" }, 400);
  }

  if (payload.action === "delete") {
    const userId = typeof payload.user_id === "string" ? payload.user_id : "";
    if (!/^[0-9a-f-]{36}$/i.test(userId)) {
      return jsonResponse({ detail: "Invalid user id" }, 422);
    }
    const { error } = await admin.auth.admin.deleteUser(userId);
    if (error) return jsonResponse({ detail: "Could not delete user" }, 502);
    return jsonResponse({ deleted: true });
  }

  if (payload.action !== "create") {
    return jsonResponse({ detail: "Invalid action" }, 422);
  }

  const email = typeof payload.email === "string" ? payload.email.trim().toLowerCase() : "";
  const password = typeof payload.password === "string" ? payload.password : "";
  const fullName = typeof payload.full_name === "string" ? payload.full_name.trim() : "";
  const businessId = Number(payload.business_id);
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)
    || fullName.length < 2
    || !Number.isInteger(businessId)
    || businessId <= 0
    || !isStrongPassword(password)) {
    return jsonResponse({ detail: "Invalid owner data" }, 422);
  }

  const { data, error } = await admin.auth.admin.createUser({
    email,
    password,
    email_confirm: true,
    user_metadata: { full_name: fullName },
    app_metadata: {
      provisioned_by: "escalar_admin",
      business_id: businessId,
    },
  });
  if (error) {
    const duplicate = /already|registered|exists/i.test(error.message);
    return jsonResponse(
      { detail: duplicate ? "User already exists" : "Could not create user" },
      duplicate ? 409 : 502,
    );
  }
  if (!data.user?.id) {
    return jsonResponse({ detail: "Missing user id" }, 502);
  }
  return jsonResponse({ id: data.user.id }, 201);
});
