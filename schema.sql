-- Core Tables
CREATE TABLE users (
    telegram_id BIGINT PRIMARY KEY,
    username TEXT,
    phone_number TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc', NOW())
);

CREATE TABLE groups (
    chat_id BIGINT PRIMARY KEY,
    group_name TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc', NOW())
);

CREATE TABLE group_members (
    group_id BIGINT REFERENCES groups(chat_id) ON DELETE CASCADE,
    user_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
    joined_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc', NOW()),
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE expenses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id BIGINT REFERENCES groups(chat_id) ON DELETE CASCADE,
    paid_by BIGINT REFERENCES users(telegram_id),
    amount DECIMAL(10, 2) NOT NULL,
    description TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc', NOW())
);

CREATE TABLE expense_splits (
    expense_id UUID REFERENCES expenses(id) ON DELETE CASCADE,
    user_id BIGINT REFERENCES users(telegram_id),
    amount_owed DECIMAL(10, 2) NOT NULL,
    PRIMARY KEY (expense_id, user_id)
);

CREATE TABLE settlements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id BIGINT REFERENCES groups(chat_id) ON DELETE CASCADE,
    from_user BIGINT REFERENCES users(telegram_id),
    to_user BIGINT REFERENCES users(telegram_id),
    amount DECIMAL(10, 2) NOT NULL,
    message_id BIGINT,
    status TEXT DEFAULT 'settled',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc', NOW())
);

-- Balance Calculation View
CREATE OR REPLACE VIEW group_user_balances AS
WITH total_paid AS (
    SELECT group_id, paid_by AS user_id, SUM(amount) AS amount_paid
    FROM expenses GROUP BY group_id, paid_by
),
total_owed AS (
    SELECT e.group_id, es.user_id, SUM(es.amount_owed) AS amount_owed
    FROM expense_splits es
    JOIN expenses e ON es.expense_id = e.id
    GROUP BY e.group_id, es.user_id
)
SELECT 
    gm.group_id, gm.user_id,
    COALESCE(tp.amount_paid, 0) AS total_paid,
    COALESCE(t_o.amount_owed, 0) AS total_owed,
    (COALESCE(tp.amount_paid, 0) - COALESCE(t_o.amount_owed, 0)) AS net_balance
FROM group_members gm
LEFT JOIN total_paid tp ON gm.group_id = tp.group_id AND gm.user_id = tp.user_id
LEFT JOIN total_owed t_o ON gm.group_id = t_o.group_id AND gm.user_id = t_o.user_id;
