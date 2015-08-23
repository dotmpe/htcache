unit-test.log::
	./unit-test | tee unit-test.log 2>&1 | tee unit-test.out

test: unit-test.log
	pass=$$(echo $$(grep '\<OK\>\ $$' $< | wc -l)); \
	grep -i '\<error\|exception\>' $< > /dev/null && { \
		fails=$$(echo $$(grep '\<ERROR\>\ $$' $< | wc -l)); \
		errs=$$(echo $$(grep -e 'error' $< | wc -l)); \
		echo "There were $$errs errors, test result: $$fails falures, $$pass pass. See $(wildcard unit-test.*)"; \
		exit 1; \
	} || { \
		echo OK, $$pass pass. No errors; \
	}

clean:
	rm *.pyc
