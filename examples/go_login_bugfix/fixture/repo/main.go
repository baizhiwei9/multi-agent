package main

import (
	"flag"
	"fmt"
	"os"
	"time"
)

// 用户白名单：只有这里面的账号能登录。
var users = map[string]string{
	"alice": "s3cret",
}

func login(user, pass string) bool {
	want, ok := users[user]
	return ok && want == pass
}

func logf(path, level, format string, args ...any) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	ts := time.Now().UTC().Format(time.RFC3339)
	fmt.Fprintf(f, "%s %s %s\n", ts, level, fmt.Sprintf(format, args...))
}

func main() {
	user := flag.String("user", "", "登录用户名")
	pass := flag.String("pass", "", "登录口令")
	logPath := flag.String("log", "server.log", "日志文件路径")
	flag.Parse()

	if login(*user, *pass) {
		logf(*logPath, "info", "登录成功 %s", *user)
		return
	}
	// 缺陷：失败日志把口令原样打出去了（应为：登录失败: <用户名>）
	logf(*logPath, "error", "登录失败 %s", *pass)
}