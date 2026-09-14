<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-cache');

$token = bot_env('BOT_API_TOKEN');
$got = $_SERVER['HTTP_X_BOT_TOKEN'] ?? '';
if (!$token || !hash_equals($token, $got)) {
    http_response_code(401);
    echo json_encode(['error' => 'unauthorized']);
    exit;
}

if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'GET') {
    http_response_code(405);
    echo json_encode(['error' => 'method']);
    exit;
}

$pdo = bot_pdo();
$id = isset($_GET['id']) ? (int) $_GET['id'] : 0;

if ($id > 0) {
    $st = $pdo->prepare(
        'SELECT chat_id, classification, status, updated_at
         FROM chats WHERE chat_id = ? AND classification IS NOT NULL'
    );
    $st->execute([$id]);
    $row = $st->fetch();
    if (!$row) {
        http_response_code(404);
        echo json_encode(['error' => 'not_found']);
        exit;
    }
    $body = json_encode($row, JSON_UNESCAPED_UNICODE);
} else {
    $rows = $pdo->query(
        'SELECT chat_id, classification, status, updated_at
         FROM chats WHERE classification IS NOT NULL
         ORDER BY updated_at DESC'
    )->fetchAll();
    $body = json_encode($rows, JSON_UNESCAPED_UNICODE);
}

$etag = '"' . sha1($body) . '"';
header('ETag: ' . $etag);
$inm = $_SERVER['HTTP_IF_NONE_MATCH'] ?? '';
if ($inm !== '' && hash_equals($etag, $inm)) {
    http_response_code(304);
    exit;
}
echo $body;
