package clean

import "context"

type Runner interface {
	Run(context.Context) error
	Shutdown(context.Context) error
}
