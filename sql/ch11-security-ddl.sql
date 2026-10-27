-- ch11: additive identity, approvals and business operations migration.
-- Back up first. Apply after ch02/ch06/ch07/ch08; never assign legacy owners automatically.
SET NAMES utf8mb4;
CREATE TABLE users (
 user_id VARCHAR(64) NOT NULL PRIMARY KEY,
 username VARCHAR(64) NOT NULL UNIQUE,
 password_hash VARCHAR(255) NOT NULL,
 role VARCHAR(16) NOT NULL DEFAULT 'user',
 is_active BOOLEAN NOT NULL DEFAULT TRUE,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE auth_families (
 id VARCHAR(32) NOT NULL PRIMARY KEY,
 user_id VARCHAR(64) NOT NULL,
 revoked_at DATETIME NULL,
 expires_at DATETIME NOT NULL,
 KEY ix_auth_families_user_id(user_id),
 FOREIGN KEY(user_id) REFERENCES users(user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE auth_sessions (
 id VARCHAR(32) NOT NULL PRIMARY KEY,
 user_id VARCHAR(64) NOT NULL,
 family_id VARCHAR(32) NOT NULL,
 refresh_token_hash VARCHAR(64) NOT NULL UNIQUE,
 expires_at DATETIME NOT NULL,
 used_at DATETIME NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 KEY ix_auth_sessions_user_id(user_id),
 KEY ix_auth_sessions_family_id(family_id),
 FOREIGN KEY(user_id) REFERENCES users(user_id),
 FOREIGN KEY(family_id) REFERENCES auth_families(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE auth_throttle (
 `key` VARCHAR(64) NOT NULL PRIMARY KEY,
 count INT NOT NULL DEFAULT 1,
 expires_at DATETIME NOT NULL,
 KEY ix_auth_throttle_expires_at(expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE orders (
 order_id VARCHAR(64) NOT NULL PRIMARY KEY,
 user_id VARCHAR(64) NOT NULL,
 status VARCHAR(32) NOT NULL,
 amount DECIMAL(12,2) NOT NULL,
 product VARCHAR(255) NOT NULL,
 tracking_no VARCHAR(64) NULL UNIQUE,
 refundable BOOLEAN NOT NULL DEFAULT FALSE,
 refund_deadline DATETIME NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 KEY ix_orders_user_id(user_id),
 FOREIGN KEY(user_id) REFERENCES users(user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE approvals (
 approval_id VARCHAR(32) NOT NULL PRIMARY KEY,
 operation_id VARCHAR(32) NOT NULL UNIQUE,
 user_id VARCHAR(64) NOT NULL,
 conversation_id BIGINT UNSIGNED NOT NULL,
 run_id VARCHAR(64) NOT NULL,
 tool_call_id VARCHAR(64) NOT NULL,
 tool_name VARCHAR(128) NOT NULL,
 tool_identity VARCHAR(512) NOT NULL,
 argument_hash VARCHAR(64) NOT NULL,
 policy_revision VARCHAR(64) NOT NULL,
 expires_at DATETIME NOT NULL,
 consumed_at DATETIME NULL,
 KEY ix_approvals_user_id(user_id),
 FOREIGN KEY(user_id) REFERENCES users(user_id),
 FOREIGN KEY(conversation_id) REFERENCES conversations(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE business_operations (
 operation_id VARCHAR(32) NOT NULL PRIMARY KEY,
 user_id VARCHAR(64) NOT NULL,
 conversation_id BIGINT UNSIGNED NOT NULL,
 kind VARCHAR(32) NOT NULL,
 argument_hash VARCHAR(64) NOT NULL,
 ticket_no VARCHAR(32) NOT NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(user_id) REFERENCES users(user_id),
 FOREIGN KEY(conversation_id) REFERENCES conversations(id),
 FOREIGN KEY(ticket_no) REFERENCES tickets(ticket_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE refund_requests (
 id VARCHAR(32) NOT NULL PRIMARY KEY,
 order_id VARCHAR(64) NOT NULL UNIQUE,
 user_id VARCHAR(64) NOT NULL,
 conversation_id BIGINT UNSIGNED NOT NULL,
 operation_id VARCHAR(32) NOT NULL UNIQUE,
 reason VARCHAR(64) NOT NULL,
 status VARCHAR(32) NOT NULL DEFAULT 'requested',
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(order_id) REFERENCES orders(order_id),
 FOREIGN KEY(user_id) REFERENCES users(user_id),
 FOREIGN KEY(conversation_id) REFERENCES conversations(id),
 FOREIGN KEY(operation_id) REFERENCES business_operations(operation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE security_decisions (
 id VARCHAR(32) NOT NULL PRIMARY KEY,
 user_id VARCHAR(64) NOT NULL,
 conversation_id BIGINT UNSIGNED NOT NULL,
 run_id VARCHAR(64) NOT NULL,
 tool_call_id VARCHAR(64) NOT NULL,
 operation_id VARCHAR(32) NOT NULL,
 tool_name VARCHAR(128) NOT NULL,
 tool_identity VARCHAR(512) NOT NULL,
 argument_hash VARCHAR(64) NOT NULL,
 policy_revision VARCHAR(64) NOT NULL,
 decision VARCHAR(16) NOT NULL,
 reason_code VARCHAR(64) NOT NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 KEY ix_security_decisions_user_created(user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

