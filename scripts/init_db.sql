-- Domain Synth Auditor — MySQL schema
-- Инициализация: mysql -u root synth_auditor < scripts/init_db.sql

CREATE TABLE IF NOT EXISTS object_types (
    id INT AUTO_INCREMENT PRIMARY KEY,
    object_type VARCHAR(255) NOT NULL UNIQUE,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS images (
    id INT AUTO_INCREMENT PRIMARY KEY,
    url TEXT NOT NULL,
    object_type_id INT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (object_type_id) REFERENCES object_types(id)
);

-- Заготовка для будущей записи результатов (пока не используется pipeline'ом)
CREATE TABLE IF NOT EXISTS results (
    id INT AUTO_INCREMENT PRIMARY KEY,
    source_image_id INT NOT NULL,
    verdict ENUM('ACCEPT', 'REJECT', 'NEEDS_REVIEW', 'ERROR') NOT NULL,
    weighted_score FLOAT,
    iterations INT,
    output_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_image_id) REFERENCES images(id)
);
