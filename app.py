import html
import re
import sqlite3
import json
import os
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'devkey-change-in-prod')
DB_PATH = os.environ.get('DB_PATH', '/data/webui.db')


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def extract_content_text(raw):
    """
    Pull a plain string out of the content column.
    Returns (is_plain_string, display_value).
    """
    if raw is None:
        return True, ''
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, str):
            return True, parsed
        return False, json.dumps(parsed, indent=2)
    except (json.JSONDecodeError, TypeError):
        return True, str(raw)


def parse_output(raw):
    """Return the output column as a Python list, or [] on failure."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _html_encode_reasoning(text):
    """
    Apply the same HTML entity encoding that Open WebUI uses on reasoning
    text before it is embedded inside the <details> block in the content
    column.

    Confirmed from database samples:
      &  ->  &amp;
      <  ->  &lt;
      >  ->  &gt;
      "  ->  &quot;
      '  ->  &#x27;

    html.escape(quote=True) covers the first four; apostrophes need a
    manual pass because Python's html module does not encode them by
    default.
    """
    escaped = html.escape(text, quote=True)
    escaped = escaped.replace("'", "&#x27;")
    return escaped


def rebuild_content_from_output(blocks):
    """
    Reconstruct the combined content string that Open WebUI stores in the
    content column for assistant messages.

    Reasoning blocks become:

        <details type="reasoning" done="true" duration="N">
        <summary>Thought for N seconds</summary>
        &gt; reasoning line one
        &gt; reasoning line two
        </details>

    Rules confirmed from live database rows:
      - The reasoning text is HTML-entity-encoded before being prefixed.
      - Every line (including blank ones) is prefixed with "&gt; "
        (ampersand-gt-semicolon-space).
      - The summary always reads "Thought for N seconds" — Open WebUI
        uses the plural even when N == 1.
      - The stored `duration` field is used directly.

    Message blocks contribute their plain text directly (no encoding).

    All parts are joined with a single newline.
    """
    parts = []

    for block in blocks:
        btype = block.get('type', '')

        # ------------------------------------------------------------------
        # Reasoning block
        # ------------------------------------------------------------------
        if btype == 'reasoning':
            reasoning_text = ''
            for item in block.get('content', []):
                if item.get('type') == 'output_text':
                    reasoning_text = item.get('text', '')
                    break

            duration = int(block.get('duration') or 0)

            encoded = _html_encode_reasoning(reasoning_text)
            quoted_lines = '\n'.join(
                f'&gt; {line}'
                for line in encoded.splitlines()
            )

            parts.append(
                f'<details type="reasoning" done="true" duration="{duration}">\n'
                f'<summary>Thought for {duration} seconds</summary>\n'
                f'{quoted_lines}\n'
                f'</details>'
            )

        # ------------------------------------------------------------------
        # Message block — plain text, no HTML encoding
        # ------------------------------------------------------------------
        elif btype == 'message':
            for item in block.get('content', []):
                if item.get('type') == 'output_text':
                    parts.append(item.get('text', ''))

    return '\n'.join(parts)


def output_blocks_for_template(blocks):
    """
    Flatten the output block list into something easy to iterate in Jinja.
    Each entry: { type, index, items: [{field, text, bi, ji}] }
    Only blocks that contain at least one output_text item are included.
    """
    result = []
    for bi, block in enumerate(blocks):
        btype = block.get('type', 'unknown')
        items = []
        for ji, item in enumerate(block.get('content', [])):
            if item.get('type') == 'output_text':
                items.append({
                    'field': f'out_{bi}_{ji}',
                    'text':  item.get('text', ''),
                    'bi': bi,
                    'ji': ji,
                })
        if items:
            result.append({'type': btype, 'index': bi, 'items': items})
    return result


def find_history_key(history_msgs, output_blocks, created_at_ms):
    """
    Find the key in history_msgs that corresponds to a chat_message row.

    The chat_message.id and the history key are different UUIDs, so we
    match on shared data instead.

    Strategy 1 — output block IDs (most reliable):
        Both the chat_message.output column and history[key].output contain
        the same API-generated block IDs (e.g. "r_abc123", "msg_xyz789").
        We collect all block IDs from the chat_message output and scan
        history entries for any match.

    Strategy 2 — timestamp fallback:
        chat_message.created_at is milliseconds; history[key].timestamp is
        seconds.  We round-trip and compare.

    Returns the matching key string, or None if not found.
    """
    # --- Strategy 1: match by output block IDs ---
    target_ids = set()
    for block in output_blocks:
        bid = block.get('id')
        if bid:
            target_ids.add(bid)

    if target_ids:
        for key, hist_msg in history_msgs.items():
            for block in hist_msg.get('output', []):
                if block.get('id') in target_ids:
                    return key

    # --- Strategy 2: timestamp fallback ---
    if created_at_ms:
        target_ts = created_at_ms // 1000
        for key, hist_msg in history_msgs.items():
            if hist_msg.get('timestamp') == target_ts:
                return key

    return None


def rebuild_messages_list(history):
    """
    Walk the history tree from root to currentId and return the flat
    messages list that Open WebUI stores in chat.chat.messages.

    The flat list is the current conversation branch in linear order.
    Tree-structure fields (id, parentId, childrenIds) are stripped out
    since the flat list doesn't use them.
    """
    messages_dict = history.get('messages', {})
    current_id    = history.get('currentId')

    if not current_id or not messages_dict:
        return []

    # Walk backwards from currentId to root, then reverse.
    path = []
    node_id = current_id
    seen = set()
    while node_id and node_id not in seen:
        if node_id not in messages_dict:
            break
        seen.add(node_id)
        path.append(node_id)
        node_id = messages_dict[node_id].get('parentId')

    path.reverse()

    _tree_keys = {'id', 'parentId', 'childrenIds'}
    result = []
    for msg_id in path:
        msg   = messages_dict[msg_id]
        entry = {k: v for k, v in msg.items() if k not in _tree_keys}
        result.append(entry)

    return result


def update_chat_column(db, chat_id, output_blocks, created_at_ms,
                       plain_content, updated_blocks):
    """
    Sync the edit back into the chat table's chat JSON column.

    Locates the correct history entry by matching output block IDs (or
    timestamp as a fallback), then updates:
      1. history.messages[key].content  — plain string
      2. history.messages[key].output   — list of blocks
      3. messages[]                     — flat list rebuilt from history tree
    """
    row = db.execute('SELECT chat FROM chat WHERE id = ?', (chat_id,)).fetchone()
    if not row or not row['chat']:
        return

    try:
        chat_data    = json.loads(row['chat'])
        history      = chat_data.get('history', {})
        history_msgs = history.get('messages', {})

        hist_key = find_history_key(history_msgs, output_blocks, created_at_ms)

        if hist_key is None:
            app.logger.warning(
                'update_chat_column: could not locate history entry for '
                'chat_id=%s — chat.history not updated', chat_id
            )
            flash('Warning: could not locate message in chat history — '
                  'chat_message table was saved but chat history was not updated.',
                  'error')
            return

        # 1 & 2 — update the history node
        history_msgs[hist_key]['content'] = plain_content
        if updated_blocks:
            history_msgs[hist_key]['output'] = updated_blocks

        history['messages']  = history_msgs
        chat_data['history'] = history

        # 3 — rebuild the flat messages list from the updated tree
        chat_data['messages'] = rebuild_messages_list(history)

        new_chat_json = json.dumps(chat_data)
        ts = int(datetime.now().timestamp() * 1000)
        db.execute(
            'UPDATE chat SET chat = ?, updated_at = ? WHERE id = ?',
            (new_chat_json, ts, chat_id)
        )

    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        app.logger.warning('Could not update chat.chat column: %s', exc)
        flash('Warning: chat history column could not be updated.', 'error')


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    q = request.args.get('q', '').strip()
    db = get_db()
    if q:
        rows = db.execute(
            'SELECT id, title, updated_at FROM chat '
            'WHERE title LIKE ? ORDER BY updated_at DESC LIMIT 200',
            (f'%{q}%',)
        ).fetchall()
    else:
        rows = db.execute(
            'SELECT id, title, updated_at FROM chat '
            'ORDER BY updated_at DESC LIMIT 200'
        ).fetchall()
    db.close()
    return render_template('index.html', chats=rows, q=q)


@app.route('/chat/<chat_id>')
def chat_view(chat_id):
    db = get_db()
    chat = db.execute('SELECT id, title FROM chat WHERE id = ?', (chat_id,)).fetchone()
    if not chat:
        flash('Chat not found.', 'error')
        db.close()
        return redirect(url_for('index'))

    msgs = db.execute(
        'SELECT id, role, content, output, created_at '
        'FROM chat_message WHERE chat_id = ? ORDER BY created_at',
        (chat_id,)
    ).fetchall()
    db.close()

    messages = []
    for m in msgs:
        _, text = extract_content_text(m['content'])
        # Strip the <details> block so the preview shows message text only
        preview_text = re.sub(
            r'<details[^>]*>.*?</details>\n?', '', text, flags=re.DOTALL
        ).strip()
        preview = (preview_text[:130] + '…') if len(preview_text) > 130 else preview_text
        messages.append({
            'id':         m['id'],
            'role':       m['role'] or 'unknown',
            'preview':    preview or '(no content)',
            'has_output': bool(m['output']),
        })

    return render_template('chat.html', chat=chat, messages=messages)


@app.route('/message/<message_id>', methods=['GET', 'POST'])
def edit_message(message_id):
    db = get_db()
    msg = db.execute('SELECT * FROM chat_message WHERE id = ?', (message_id,)).fetchone()
    if not msg:
        flash('Message not found.', 'error')
        db.close()
        return redirect(url_for('index'))

    chat_id = msg['chat_id']

    if request.method == 'POST':
        original_blocks = parse_output(msg['output'])

        # --- Rebuild output JSON with the edited text values from the form ---
        updated_blocks = []
        for bi, block in enumerate(original_blocks):
            new_block = dict(block)
            if 'content' in block:
                new_items = []
                for ji, item in enumerate(block['content']):
                    new_item = dict(item)
                    if item.get('type') == 'output_text':
                        key = f'out_{bi}_{ji}'
                        if key in request.form:
                            new_item['text'] = request.form[key]
                    new_items.append(new_item)
                new_block['content'] = new_items
            updated_blocks.append(new_block)

        # --- Rebuild content column ---
        #
        # plain_content  : the human-readable string stored in chat.history
        # new_content    : JSON-encoded version stored in chat_message.content
        #
        role     = msg['role'] or ''
        is_plain, _ = extract_content_text(msg['content'])

        if role == 'assistant' and updated_blocks:
            plain_content = rebuild_content_from_output(updated_blocks)
            new_content   = json.dumps(plain_content)
        else:
            plain_content = request.form.get('content_text', '')
            if is_plain:
                new_content = json.dumps(plain_content)
            else:
                try:
                    new_content = json.dumps(json.loads(plain_content))
                except json.JSONDecodeError:
                    flash('Content field is not valid JSON — changes not saved.', 'error')
                    db.close()
                    return redirect(url_for('edit_message', message_id=message_id))

        new_output   = json.dumps(updated_blocks) if updated_blocks else msg['output']
        created_at_ms = msg['created_at']
        ts = int(datetime.now().timestamp() * 1000)

        # --- Save chat_message row ---
        db.execute(
            'UPDATE chat_message SET content = ?, output = ?, updated_at = ? WHERE id = ?',
            (new_content, new_output, ts, message_id)
        )

        # --- Sync the chat table's JSON column ---
        # Pass the original blocks (before edit) for ID-based matching, plus
        # the updated blocks to write back.
        update_chat_column(
            db, chat_id,
            original_blocks, created_at_ms,
            plain_content, updated_blocks
        )

        db.commit()
        db.close()
        flash('Message saved.', 'success')
        return redirect(url_for('edit_message', message_id=message_id))

    # GET — prepare display values
    is_plain, content_text = extract_content_text(msg['content'])
    raw_blocks  = parse_output(msg['output'])
    tmpl_blocks = output_blocks_for_template(raw_blocks)

    db.close()
    return render_template(
        'edit_message.html',
        msg=msg,
        chat_id=chat_id,
        content_text=content_text,
        is_plain=is_plain,
        tmpl_blocks=tmpl_blocks,
    )


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=5000, debug=debug)