CREATE TABLE IF NOT EXISTS public.bot_credits (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  bot_id uuid NOT NULL REFERENCES public.bot_orders(id) ON DELETE CASCADE,
  guild_id text NOT NULL,
  user_id text NOT NULL,
  amount integer NOT NULL,
  reason text,
  granted_by text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS bot_credits_lookup_idx
  ON public.bot_credits (bot_id, guild_id, user_id, created_at DESC);

ALTER TABLE public.bot_credits ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS bot_credits_owner_select ON public.bot_credits;
CREATE POLICY bot_credits_owner_select ON public.bot_credits
  FOR SELECT USING (
    EXISTS (
      SELECT 1 FROM public.bot_orders o
      WHERE o.id = bot_credits.bot_id
        AND (o.user_id = auth.uid() OR public.has_role(auth.uid(), 'admin'))
    )
  );

DROP POLICY IF EXISTS bot_credits_service_all ON public.bot_credits;
CREATE POLICY bot_credits_service_all ON public.bot_credits
  FOR ALL USING (auth.role() = 'service_role') WITH CHECK (auth.role() = 'service_role');

CREATE TABLE IF NOT EXISTS public.bot_tickets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  bot_id uuid NOT NULL REFERENCES public.bot_orders(id) ON DELETE CASCADE,
  guild_id text NOT NULL,
  channel_id text NOT NULL,
  opener_id text,
  category text,
  status text NOT NULL DEFAULT 'open',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (bot_id, channel_id)
);

CREATE INDEX IF NOT EXISTS bot_tickets_lookup_idx
  ON public.bot_tickets (bot_id, guild_id, status, created_at DESC);

ALTER TABLE public.bot_tickets ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS bot_tickets_owner_select ON public.bot_tickets;
CREATE POLICY bot_tickets_owner_select ON public.bot_tickets
  FOR SELECT USING (
    EXISTS (
      SELECT 1 FROM public.bot_orders o
      WHERE o.id = bot_tickets.bot_id
        AND (o.user_id = auth.uid() OR public.has_role(auth.uid(), 'admin'))
    )
  );

DROP POLICY IF EXISTS bot_tickets_service_all ON public.bot_tickets;
CREATE POLICY bot_tickets_service_all ON public.bot_tickets
  FOR ALL USING (auth.role() = 'service_role') WITH CHECK (auth.role() = 'service_role');

CREATE OR REPLACE FUNCTION public.runtime_credits_op(
  _token text,
  _bot_id uuid,
  _op text,
  _payload jsonb DEFAULT '{}'::jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $$
DECLARE
  _tok RECORD;
  _guild text;
  _user text;
  _amount integer;
  _total integer;
  _entries jsonb;
BEGIN
  SELECT * INTO _tok FROM public._worker_token_lookup(_token) LIMIT 1;
  IF _tok.token_id IS NULL THEN RAISE EXCEPTION 'invalid_token'; END IF;
  IF _tok.bot_id IS NOT NULL AND _tok.bot_id <> _bot_id THEN RAISE EXCEPTION 'token_bot_mismatch'; END IF;

  _guild := _payload->>'guild_id';
  _user := _payload->>'user_id';

  IF _op = 'add' THEN
    _amount := COALESCE((_payload->>'amount')::integer, 0);
    INSERT INTO public.bot_credits (bot_id, guild_id, user_id, amount, reason, granted_by)
    VALUES (_bot_id, _guild, _user, _amount, _payload->>'reason', _payload->>'granted_by');
    SELECT COALESCE(SUM(amount), 0) INTO _total
      FROM public.bot_credits
     WHERE bot_id = _bot_id AND guild_id = _guild AND user_id = _user;
    RETURN jsonb_build_object('ok', true, 'total', _total);

  ELSIF _op = 'balance' THEN
    SELECT COALESCE(SUM(amount), 0) INTO _total
      FROM public.bot_credits
     WHERE bot_id = _bot_id AND guild_id = _guild AND user_id = _user;
    SELECT COALESCE(jsonb_agg(e ORDER BY e.created_at DESC), '[]'::jsonb) INTO _entries
      FROM (
        SELECT amount, reason, granted_by, created_at
          FROM public.bot_credits
         WHERE bot_id = _bot_id AND guild_id = _guild AND user_id = _user
         ORDER BY created_at DESC
         LIMIT 25
      ) e;
    RETURN jsonb_build_object('ok', true, 'total', _total, 'entries', _entries);

  ELSIF _op = 'ticket_log' THEN
    INSERT INTO public.bot_tickets (bot_id, guild_id, channel_id, opener_id, category, status, updated_at)
    VALUES (_bot_id, _guild, _payload->>'channel_id', _payload->>'opener_id', _payload->>'category', COALESCE(_payload->>'status', 'open'), now())
    ON CONFLICT (bot_id, channel_id) DO UPDATE
      SET status = EXCLUDED.status, updated_at = now();
    RETURN jsonb_build_object('ok', true);
  END IF;

  RETURN jsonb_build_object('ok', false, 'error', 'unknown_op');
END;
$$;

REVOKE ALL ON FUNCTION public.runtime_credits_op(text, uuid, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.runtime_credits_op(text, uuid, text, jsonb) TO anon, authenticated, service_role;
